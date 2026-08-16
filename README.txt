DEPLOY TO log2.gayathiriportfolio.xyz — STEP BY STEP
======================================================

STEP 1 — Get a Google Cloud API key (free tier)
-------------------------------------------------
1. Go to https://console.cloud.google.com/ and create a project (or use an existing one).
2. Enable these 3 APIs (search each by name in "APIs & Services > Library"):
   - Cloud Vision API
   - Geocoding API
   - Places API
3. Go to "APIs & Services > Credentials" > "Create Credentials" > "API key".
4. Copy the key. Optionally restrict it to just those 3 APIs for safety.
5. Google requires a billing account attached even for free-tier usage,
   but Vision (1000 units/month), Geocoding, and Places all have monthly
   free credit that covers light testing/demo use.

STEP 2 — Create the subdomain (if not already done)
-------------------------------------------------
1. In cPanel > Domains > Create A New Domain, add: log2.gayathiriportfolio.xyz
2. Note the document root it creates, e.g. /home/USERNAME/log2.gayathiriportfolio.xyz

STEP 3 — Set up the Python App
-------------------------------------------------
1. In cPanel, open "Setup Python App".
2. Click "Create Application".
   - Python version: 3.9+ (whatever's available)
   - Application root: log2.gayathiriportfolio.xyz  (or wherever you want the code)
   - Application URL: log2.gayathiriportfolio.xyz
   - Application startup file: passenger_wsgi.py
   - Application Entry point: application
3. Click Create.

STEP 4 — Upload the files
-------------------------------------------------
1. Unzip this package.
2. In cPanel File Manager, go to the Application root folder from Step 3.
3. Upload app.py, passenger_wsgi.py, requirements.txt, and the static/ folder
   (with index.html inside it) into that folder.

STEP 5 — Install dependencies
-------------------------------------------------
1. Back in "Setup Python App", find your app and click the "..." menu.
2. There's a command box shown at the top like:
   source /home/USERNAME/virtualenv/log2.../3.9/bin/activate && cd ~/log2...
3. SSH into your server (or use cPanel Terminal) and run that activate command,
   then run:
   pip install -r requirements.txt

STEP 6 — Set your API key as an environment variable
-------------------------------------------------
1. In "Setup Python App", edit your app.
2. Under "Environment variables" add:
   Name: GOOGLE_API_KEY
   Value: <paste your key from Step 1>
3. Save, then click "Restart".

STEP 7 — Test it
-------------------------------------------------
1. Visit https://log2.gayathiriportfolio.xyz/
2. Upload a few property photos, optionally type an address.
3. Click "Analyze Property" — you should see a real results table.

NOTES / HONEST LIMITATIONS
-------------------------------------------------
- "Property Condition" grading (Excellent/Above Average/etc.) is NOT included
  here because that needs a custom-trained classifier on labeled photos —
  no pretrained free API gives a reliable condition grade. Everything shown
  is a real detection, not a guess.
- Waterfront, highway proximity, and gated-community detection are marked
  "Not available on free tier" — Google's free APIs don't reliably answer these.
- If you later want the condition grading, that's Phase 3: collect ~200-500
  labeled property photos (condition already known) and train a small
  classifier (e.g. with a service like Google AutoML Vision, or fine-tuning
  a small model), then swap it in as another Vision-style API call.
