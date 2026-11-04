import sys
from pathlib import Path

# The agent scripts import each other by name (python agent/buyer.py puts agent/ on the path)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))