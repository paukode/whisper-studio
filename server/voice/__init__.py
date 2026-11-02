"""Hands-free voice conversation mode backed by Amazon Nova 2 Sonic.

Layout (each module is importable without the Bedrock streaming SDK installed;
only ``sonic_client`` touches it, lazily):

  protocol.py      pure builders/parsers for the Sonic bidirectional event protocol
  sonic_client.py  thin wrapper over aws_sdk_bedrock_runtime's duplex stream
  tools.py         the small voice tool set (ask_assistant delegates to the
                   normal chat engine, so Claude stays the brain)
  session.py       one live voice conversation: stream lifecycle, transcript,
                   tool dispatch, 8-minute stream renewal
  routes.py        /ws/voice (browser <-> session) and /api/voice/status
"""
