import os

# The CLI's Rich consoles are created at import time and read FORCE_COLOR then; with it
# set they emit ANSI codes that break assertions on output text. conftest is imported
# before the test modules, so clearing it here takes effect in time.
os.environ.pop("FORCE_COLOR", None)
