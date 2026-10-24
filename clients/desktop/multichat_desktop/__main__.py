import sys

if "--approval-mcp" in sys.argv:
    # The frozen MultiChat.exe doubles as the approval bridge Claude Code starts (see runner/cli_agents.py).
    from multichat_client.runner.approval_mcp import main as bridge

    bridge()
else:
    from multichat_desktop.app import main

    sys.exit(main())
