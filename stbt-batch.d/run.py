#!/usr/bin/env python

# Copyright 2015 stb-tester.com Ltd.
# License: LGPL v2.1 or (at your option) any later version (see
# https://github.com/stb-tester/stb-tester/blob/master/LICENSE for details).


import argparse
import datetime
import errno
import os
import random
import signal
import subprocess
import sys
import time
from collections import namedtuple
from contextlib import contextmanager
from distutils.spawn import find_executable

from _stbt.state_watch import new_state_sender
from _stbt.utils import mkdir_p


def main(argv):
    parser = argparse.ArgumentParser(usage=(
        "\n  stbt batch run [options] test.py [test.py ...]"
        "\n  stbt batch run [options] test.py arg [arg ...] -- "
        "test.py arg [arg ...] [-- ...])"))
    parser.add_argument(
        '-1', '--run-once', action="store_true", help=(
            'Run once. The default behaviour is to run the test repeatedly as '
            'long as it passes.'))
    parser.add_argument(
        '-k', '--keep-going', action="count", help=(
            'Continue running after failures.  Provide this argument once to '
            'continue running after "uninteresting" failures, and twice to '
            'continue running after any failure (except those that would '
            'prevent any further test from passing).'))
    parser.add_argument(
        '-d', '--debug', action="store_true", help=(
            'Enable "stbt-debug" dump of intermediate images.'))
    parser.add_argument(
        '-v', '--verbose', action="count", default=0, help=(
            'Verbose. Provide this argument once to print stbt standard '
            'output. Provide this argument twice to also print stbt stderr '
            'output.'))
    parser.add_argument(
        '-o', '--output', default=os.curdir, help=(
            'Output directory to save the report and test-run logs under '
            '(defaults to the current directory).'))
    parser.add_argument(
        '-t', '--tag', help=(
            'Tag to add to test-run directory names (useful to differentiate '
            'directories when you intend to merge test results from multiple '
            'machines).'))
    parser.add_argument(
        '--shuffle', action="store_true", help=(
            "Run the test cases in a random order attempting to spend the same "
            "total amount of time executing each test case."))
    parser.add_argument(
        '--no-html-report', action='store_false', dest='do_html_report',
        help="""Don't generate an HTML report after each test-run; generating
            the report can be slow if there are many results in the output
            directory. You can still generate the HTML reports afterwards with
            'stbt batch report'.""")
    parser.add_argument(
        '--no-save-video', action='store_false', dest='do_save_video', help="""
            Don't generate a video recording of each test-run. Use this if you
            are saving video another way.""")
    parser.add_argument('test_name', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv[1:])

    if args.tag is not None:
        tag = '-' + args.tag
    else:
        tag = ""

    os.environ['PYTHONUNBUFFERED'] = 'x'

    term_count = [0]

    def on_term(_signo, _frame):
        term_count[0] += 1
        if term_count[0] == 1:
            sys.stderr.write(
                "\nReceived interrupt; waiting for current test to complete.\n")
        else:
            sys.stderr.write("Received interrupt; exiting.\n")
            sys.exit(1)

    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGTERM, on_term)

    failure_count = 0
    last_exit_status = 0

    if not find_executable('ts'):
        sys.stderr.write(
            "No 'ts' command found; please install 'moreutils' package\n")
        return 1

    test_cases = parse_test_args(args.test_name)

    run_count = 0

    if args.shuffle:
        test_generator = shuffle(test_cases, repeat=not args.run_once)
    else:
        test_generator = loop_tests(test_cases, repeat=not args.run_once)

    mkdir_p(args.output)
    state_sender = new_state_sender()

    # We assume that all test-cases are in the same git repo:
    git_info = read_git_info(os.path.dirname(test_cases[0][0]))

    for test_name, test_args in test_generator:
        if term_count[0] > 0:
            break
        run_count += 1

        with setup_dirs(args.output, tag, state_sender) as rundir:
            fill_in_data_files(rundir, test_name, test_args, git_info, args.tag)
            last_exit_status = run_one(test_name, test_args, args, cwd=rundir)

        if last_exit_status != 0:
            failure_count += 1
        if os.path.exists(
                "%s/latest%s/unrecoverable-error" % (args.output, tag)):
            break

        if last_exit_status == 0:
            continue
        elif last_exit_status >= 2 and args.keep_going > 0:
            # "Uninteresting" failures due to the test infrastructure
            continue
        elif args.keep_going >= 2:
            continue
        else:
            break

    if run_count == 1:
        # If we only run a single test a single time propagate the result
        # through
        return last_exit_status
    elif failure_count == 0:
        return 0
    else:
        return 1


GitInfo = namedtuple('GitInfo', 'commit commit_sha git_dir')


def read_git_info(testdir):
    try:
        def git(*cmd):
            return subprocess.check_output(('git',) + cmd, cwd=testdir).strip()
        return GitInfo(
            git('describe', '--always', '--dirty'),
            git('rev-parse', 'HEAD'),
            git('rev-parse', '--show-toplevel'))
    except subprocess.CalledProcessError:
        return None
    except OSError as e:
        # ENOENT means that git is not in $PATH
        if e.errno != errno.ENOENT:
            raise


def make_rundir(outputdir, tag):
    for n in range(2):
        rundir = datetime.datetime.now().strftime("%Y-%m-%d_%H.%M.%S") + tag
        try:
            os.mkdir(os.path.join(outputdir, rundir))
            return rundir
        except OSError as e:
            if e.errno == errno.EEXIST and n == 0:
                # Avoid directory name clashes if the previous test took <1s to
                # run
                time.sleep(1)
            else:
                raise


def fill_in_data_files(rundir, test_name, test_args, git_info, tag):
    def write_file(name, data):
        with open(os.path.join(rundir, name), 'w') as f:
            f.write(data)

    if git_info:
        write_file("git-commit", git_info.commit)
        write_file("git-commit-sha", git_info.commit_sha)
        write_file("test-name", os.path.relpath(test_name, git_info.git_dir))
    else:
        write_file("test-name", os.path.abspath(test_name))
    write_file("test-args", "\n".join(test_args))

    if tag:
        write_file("extra-columns", "Tag\t%s\n" % tag)


@contextmanager
def setup_dirs(outputdir, tag, state_sender):
    mkdir_p(outputdir)

    rundir = make_rundir(outputdir, tag)

    symlink_f(rundir, os.path.join(outputdir, "current" + tag))

    state_sender.set({
        "active_results_directory":
        os.path.abspath(os.path.join(outputdir, rundir))})
    try:
        yield os.path.join(outputdir, rundir)
    finally:
        # Now test has finished...
        symlink_f(rundir, os.path.join(outputdir, "latest" + tag))
        state_sender.set({"active_results_directory": None})


def symlink_f(source, link_name):
    name = "%s-%06i~" % (link_name, random.randint(0, 999999))
    os.symlink(source, name)
    os.rename(name, link_name)


DEVNULL_R = open('/dev/null')


def run_one(test_name, test_args, batch_args, cwd):
    """
    Invoke the run-one shell-script with the appropriate arguments.
    """

    cmd = [_find_file('../stbt-run'), '--save-thumbnail=always']
    if batch_args.do_save_video:
        cmd += ['--save-video=video.webm']
    if batch_args.debug:
        cmd += ['-vv']
    else:
        cmd += ['-v']
    cmd += [os.path.abspath(test_name), '--'] + list(test_args)

    subenv = dict(os.environ)
    subenv['do_html_report'] = "true" if batch_args.do_html_report else "false"
    subenv['stbt_root'] = _find_file('..')
    subenv['test_displayname'] = " ".join((test_name,) + test_args)
    subenv['verbose'] = str(batch_args.verbose)
    child = None
    try:
        child = subprocess.Popen(
            [_find_file("run-one")] + cmd, stdin=DEVNULL_R,
            env=subenv, preexec_fn=lambda: os.setpgid(0, 0), cwd=cwd)
        return child.wait()
    except SystemExit:
        if child:
            os.kill(-child.pid, signal.SIGTERM)
            child.wait()
        raise


def listsplit(l, v):
    """
    A bit like str.split, but for lists

    >>> listsplit(['test 1', '--', 'test 2', 'arg1', '--', 'test3'], '--')
    [['test 1'], ['test 2', 'arg1'], ['test3']]
    """
    out = []
    sublist = []
    for x in l:
        if x == v:
            if sublist:
                out.append(sublist)
            sublist = []
        else:
            sublist.append(x)
    if sublist:
        out.append(sublist)
    return out


def parse_test_args(args):
    """
    >>> parse_test_args(['test 1.py', 'test2.py', 'test3.py'])
    [('test 1.py', ()), ('test2.py', ()), ('test3.py', ())]
    >>> parse_test_args(['test1.py', 'test2.py'])
    [('test1.py', ()), ('test2.py', ())]
    >>> parse_test_args(['test1.py', '--'])
    [('test1.py', ())]
    >>> parse_test_args(['test1.py', '--', 'test2.py'])
    [('test1.py', ()), ('test2.py', ())]
    >>> parse_test_args(['test1.py', '--', 'test2.py', '--'])
    [('test1.py', ()), ('test2.py', ())]
    >>> parse_test_args(['test1.py', 'test2.py'])
    [('test1.py', ()), ('test2.py', ())]
    >>> parse_test_args(
    ...     ['test1.py', 'arg1', 'arg2', '--', 'test2.py', 'arg', '--',
    ...      'test3.py'])
    [('test1.py', ('arg1', 'arg2')), ('test2.py', ('arg',)), ('test3.py', ())]
    """
    if '--' in args:
        return [(x[0], tuple(x[1:])) for x in listsplit(args, '--')]
    else:
        return [(x, ()) for x in args]


def loop_tests(test_cases, repeat=True):
    while True:
        for test in test_cases:
            yield test
        if not repeat:
            return


def weighted_choice(choices):
    """
    See http://stackoverflow.com/questions/3679694/
    """
    total = sum(w for c, w in choices)
    r = random.uniform(0, total)
    upto = 0
    for c, w in choices:
        if upto + w > r:
            return c
        upto += w
    assert False, "Shouldn't get here"


def shuffle(test_cases, repeat=True):
    test_cases = test_cases[:]
    random.shuffle(test_cases)
    timings = {test: [0.0, 0] for test in test_cases}

    # Run all the tests first time round:
    for test in test_cases:
        start_time = time.time()
        yield test
        timings[test][0] += time.time() - start_time
        timings[test][1] += 1

    if not repeat:
        return

    while True:
        test = weighted_choice([(k, v[1] / v[0]) for k, v in timings.items()])
        start_time = time.time()
        yield test
        timings[test][0] += time.time() - start_time
        timings[test][1] += 1


def test_that_shuffle_runs_through_all_tests_initially_with_repeat():
    from itertools import islice

    test_cases = range(20)
    out = list(islice(shuffle(test_cases), 20))

    # They must be randomised:
    assert test_cases != out

    # But all of them must have been run
    assert test_cases == sorted(out)


def test_that_shuffle_runs_through_all_tests_no_repeat():
    test_cases = range(20)
    out = list(shuffle(test_cases, repeat=False))

    # They must be randomised:
    assert test_cases != out

    # But all of them must have been run
    assert test_cases == sorted(out)


def test_that_shuffle_equalises_time_across_tests():
    from mock import patch
    faketime = [0.0]

    def mytime():
        return faketime[0]

    test_cases = [
        ("test1", 20),
        ("test2", 10),
        ("test3", 5),
    ]

    time_spent_in_test = {
        "test1": 0,
        "test2": 0,
        "test3": 0,
    }

    def fake_run_test(testcase):
        time_spent_in_test[testcase[0]] += testcase[1]
        faketime[0] += testcase[1]

    with patch('time.time', mytime):
        generator = shuffle(test_cases)
        while faketime[0] < 100000:
            fake_run_test(generator.next())

    print time_spent_in_test

    assert 30000 < time_spent_in_test["test1"] < 36000
    assert 30000 < time_spent_in_test["test2"] < 36000
    assert 30000 < time_spent_in_test["test3"] < 36000


def _find_file(path, root=os.path.dirname(os.path.abspath(__file__))):
    return os.path.join(root, path)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
