configuration=
{
  daemon=true,
  pathSeparator="/",
  logAppenders=
  {
    {
      name="file appender",
      type="file",
      level=6,
      fileName="/var/log/crtmpserver/main.log",
    }
  },
  applications=
  {
    rootDirectory="/usr/lib/crtmpserver/applications",
    {
      description="FLV Playback",
      name="flvplayback",
      protocol="dynamiclinklibrary",
      default=true,
      aliases=
      {
        "vod",
        "live",
      },
      acceptors =
      {
        {
          ip="0.0.0.0",
          port=1935,
          protocol="inboundRtmp"
        },
      },
      validateHandshake=false,
    },
  }
}
