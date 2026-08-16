import sys
import os

# Add app directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

# If cPanel doesn't auto-activate your virtualenv, uncomment and edit below:
# INTERP = os.path.expanduser("~/virtualenv/log2/3.11/bin/python")
# if sys.executable != INTERP:
#     os.execl(INTERP, INTERP, *sys.argv)

# Import Flask app as 'application' (Passenger WSGI standard)
from app import app as application