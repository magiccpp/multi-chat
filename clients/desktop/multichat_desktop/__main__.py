import sys

if "--approval-mcp" in sys.argv:
    # The frozen MultiChat.exe doubles as the approval bridge Claude Code starts (see runner/cli_agents.py).
    from multichat_client.runner.approval_mcp import main as bridge

    bridge()
elif "--smoke-test" in sys.argv:
    # Used by CI on the frozen exe: build the main window offscreen and exit 0 if everything loads.
    from multichat_desktop.app import smoke_test

    sys.exit(smoke_test())
else:
    from multichat_desktop.app import main

    sys.exit(main())
