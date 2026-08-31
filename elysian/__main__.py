"""Package entry point: python -m elysian"""
import sys

from .host import run

if __name__ == "__main__":
    sys.exit(run())
