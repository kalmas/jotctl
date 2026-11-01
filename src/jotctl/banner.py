"""The block-letter banner shown above `jotctl --help`."""

from rich.console import Console
from rich.text import Text

ROWS = [
    "     ██╗ ██████╗ ████████╗ ██████╗████████╗██╗     ",
    "     ██║██╔═══██╗╚══██╔══╝██╔════╝╚══██╔══╝██║     ",
    "     ██║██║   ██║   ██║   ██║        ██║   ██║     ",
    "██   ██║██║   ██║   ██║   ██║        ██║   ██║     ",
    "╚█████╔╝╚██████╔╝   ██║   ╚██████╗   ██║   ███████╗",
    " ╚════╝  ╚═════╝    ╚═╝    ╚═════╝   ╚═╝   ╚══════╝",
]
# Top-to-bottom gradient: violet → blue → cyan.
COLORS = ["#a855f7", "#8b5cf6", "#6366f1", "#3b82f6", "#0ea5e9", "#06b6d4"]
TAGLINE = "kubectl-style control for your Jotform forms"
WIDTH = len(ROWS[0])


def print_banner(console: Console) -> None:
    """Print the banner, or nothing if the terminal is too narrow to show it intact."""
    if console.width < WIDTH + 2:
        return
    console.print()
    for row, color in zip(ROWS, COLORS, strict=True):
        console.print(Text(f" {row}", style=f"bold {color}"), soft_wrap=True)
    console.print(Text(f" {TAGLINE}".center(WIDTH + 1), style="dim italic"))
