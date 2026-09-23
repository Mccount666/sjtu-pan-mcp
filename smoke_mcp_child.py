"""Child process entry: run the sjtu-pan MCP stdio server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sjtu_pan_mcp.server import main

if __name__ == "__main__":
    main()
