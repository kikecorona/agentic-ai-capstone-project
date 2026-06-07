"""Seed data for the catalog service.

Roughly fifty fake apps spread across categories. Editing this file is
the supported way to add or change apps in the demo.
"""
from shared.models import App


SEED_APPS = [
    App("app.001", "Skyline Weather",      "Nimbus Labs",        "Weather",        0,    "Hyperlocal forecasts with vibe-coded animations.", "🌤️", 4.8, 12453, "com.nimbus.skyline", "3.2.1", 84),
    App("app.002", "Pocket Ledger",        "Forge & Quill",      "Finance",        499,  "Double-entry bookkeeping that fits in your pocket.", "📒", 4.6, 8821,  "com.forge.ledger",   "2.0.0", 22),
    App("app.003", "Loop Runner",          "Cadence Studio",     "Health",         0,    "Interval training with adaptive coaching.",        "🏃", 4.5, 30210, "com.cadence.runner", "5.1.4", 110),
    App("app.004", "Pixel Atlas",          "Sextant Software",   "Navigation",     999,  "Offline maps with hand-drawn cartography.",        "🗺️", 4.7, 4502,  "com.sextant.atlas",  "1.4.0", 320),
    App("app.005", "Quill Notes",          "Forge & Quill",      "Productivity",   0,    "Markdown notes that sync over iCloud.",            "📝", 4.4, 19834, "com.forge.quill",    "4.5.2", 41),
    App("app.006", "Stardust Sleep",       "Lumen Health",       "Health",         299,  "Soundscapes engineered for deep sleep.",           "🌙", 4.3, 6711,  "com.lumen.sleep",    "1.8.0", 62),
    App("app.007", "Bonsai Habits",        "Tidepool Co.",       "Lifestyle",      199,  "Tend tiny trees by keeping good habits.",          "🌳", 4.6, 9842,  "com.tidepool.bonsai","2.1.0", 35),
    App("app.008", "Velocity DAW",         "Octave Forge",       "Music",          1999, "Modular pro audio workstation.",                   "🎚️", 4.9, 2031,  "com.octave.velocity","6.0.0", 540),
    App("app.009", "Reef Explorer",        "Tidepool Co.",       "Education",      0,    "An interactive guide to coral ecosystems.",        "🐠", 4.5, 3320,  "com.tidepool.reef",  "1.2.0", 180),
    App("app.010", "Starlit Camera",       "Aurora Optics",      "Photo & Video",  399,  "Astrophotography with stacking on-device.",        "📷", 4.7, 5119,  "com.aurora.starlit", "3.0.1", 95),
    App("app.011", "Compass Mail",         "Sextant Software",   "Productivity",   0,    "Calm, focused email client.",                      "✉️", 4.2, 8800,  "com.sextant.mail",   "2.4.0", 70),
    App("app.012", "Klangform",            "Octave Forge",       "Music",          299,  "Generative ambient music composer.",               "🎼", 4.6, 1842,  "com.octave.klangform","1.0.5", 60),
    App("app.013", "Lumen Reader",         "Lumen Health",       "Books",          0,    "Distraction-free e-reader with read-aloud.",       "📖", 4.4, 11120, "com.lumen.reader",   "3.7.0", 50),
    App("app.014", "Drift Browser",        "Mariner Inc.",       "Utilities",      0,    "Privacy-first web browser.",                       "🌊", 4.3, 14201, "com.mariner.drift",  "117.0",  150),
    App("app.015", "Beacon Dev",           "Mariner Inc.",       "Developer Tools",1499, "iOS device console + log viewer.",                 "📡", 4.8, 902,   "com.mariner.beacon", "2.2.0", 200),
    App("app.016", "Threadbare RPG",       "Lantern Games",      "Games",          499,  "Cozy turn-based fantasy roguelike.",               "🎲", 4.8, 22431, "com.lantern.threadbare","1.6.0",420),
    App("app.017", "Photon Racer",         "Lantern Games",      "Games",          0,    "Tilt-controlled neon racer.",                      "🏎️", 4.2, 88102, "com.lantern.photon", "4.0.0", 230),
    App("app.018", "Glade",                "Tidepool Co.",       "Health",         0,    "Daily mood + journaling check-in.",                "🌿", 4.7, 17420, "com.tidepool.glade", "2.0.0", 45),
    App("app.019", "Saltline Recipes",     "Brine & Yeast",      "Food & Drink",   0,    "Recipes from coastal kitchens.",                   "🥗", 4.5, 4220,  "com.brine.saltline", "1.3.0", 65),
    App("app.020", "Ferment",              "Brine & Yeast",      "Food & Drink",   299,  "Track sourdough starters and pickles.",            "🥖", 4.4, 1540,  "com.brine.ferment",  "1.0.2", 30),
    App("app.021", "Kite Code",            "Helio Tools",        "Developer Tools",0,    "Pair-programming sketchpad with diagrams.",        "🪁", 4.5, 1308,  "com.helio.kite",     "0.9.4", 80),
    App("app.022", "Tessera Tasks",        "Helio Tools",        "Productivity",   199,  "Project planning with kanban + timeline.",         "🧩", 4.3, 6212,  "com.helio.tessera",  "5.4.0", 95),
    App("app.023", "Hush",                 "Lumen Health",       "Lifestyle",      0,    "White noise + breath pacing.",                     "🤫", 4.6, 23110, "com.lumen.hush",     "2.2.1", 40),
    App("app.024", "Ridge Hike",           "Sextant Software",   "Travel",         499,  "Trail maps + GPS for backcountry hikers.",         "⛰️", 4.7, 3340,  "com.sextant.ridge",  "2.0.0", 210),
    App("app.025", "Foxglove Finance",     "Forge & Quill",      "Finance",        0,    "Investment tracking with tax lot detail.",         "🦊", 4.5, 5611,  "com.forge.foxglove", "3.4.1", 38),
    App("app.026", "Polaris Translate",    "Aurora Optics",      "Reference",      0,    "Camera-based real-time translation.",              "🌐", 4.2, 7320,  "com.aurora.polaris", "1.7.0", 180),
    App("app.027", "Snowdrop",             "Tidepool Co.",       "Education",      199,  "Spaced repetition flashcards for kids.",           "❄️", 4.5, 2210,  "com.tidepool.snowdrop","1.1.0",60),
    App("app.028", "Echo Podcasts",        "Mariner Inc.",       "News",           0,    "Smart podcast catcher with chapter AI.",           "🎙️", 4.4, 14210, "com.mariner.echo",   "4.2.0", 90),
    App("app.029", "Carve",                "Octave Forge",       "Photo & Video",  999,  "Non-linear video editor for shorts.",              "🎞️", 4.6, 1820,  "com.octave.carve",   "1.5.0", 480),
    App("app.030", "Lantern Clock",        "Lantern Games",      "Utilities",      0,    "Analog world clock with auras.",                   "🕰️", 4.1, 9201,  "com.lantern.clock",  "2.0.0", 25),
    App("app.031", "Driftwood",            "Mariner Inc.",       "Books",          0,    "Cozy short-fiction reader.",                       "📚", 4.3, 2210,  "com.mariner.driftwood","1.2.0",60),
    App("app.032", "Solstice Yoga",        "Lumen Health",       "Health",         499,  "Daily flows for every level.",                     "🧘", 4.6, 7702,  "com.lumen.solstice", "3.0.0", 320),
    App("app.033", "Almanac Garden",       "Brine & Yeast",      "Lifestyle",      299,  "Plan and track a backyard garden.",                "🌻", 4.5, 1801,  "com.brine.almanac",  "1.0.0", 70),
    App("app.034", "Cinder",               "Helio Tools",        "Developer Tools",1999, "Native Git client with mergefriend AI.",           "🔥", 4.7, 612,   "com.helio.cinder",   "2.0.0", 220),
    App("app.035", "Loomwork",             "Forge & Quill",      "Productivity",   999,  "Outlines, mind maps, and writing in one place.",   "🧶", 4.7, 4400,  "com.forge.loomwork", "2.5.0", 130),
    App("app.036", "Spire Architect",      "Sextant Software",   "Productivity",   2999, "Pro-grade floor plan + 3D walkthroughs.",          "🏗️", 4.8, 230,   "com.sextant.spire",  "8.0.0", 700),
    App("app.037", "Cumulus Notes",        "Nimbus Labs",        "Productivity",   0,    "Notes that travel with the weather.",              "☁️", 4.0, 1102,  "com.nimbus.cumulus", "1.1.0", 30),
    App("app.038", "Hollow Knight: Mobile","Lantern Games",      "Games",          1499, "Side-scrolling action adventure.",                 "🪲", 4.9, 41200, "com.lantern.hollowknight","1.0.0",950),
    App("app.039", "Stitch Studio",        "Aurora Optics",      "Photo & Video",  299,  "Panorama + photo merging utility.",                "🪡", 4.4, 980,   "com.aurora.stitch",  "1.3.0", 75),
    App("app.040", "Tide Calendar",        "Mariner Inc.",       "Productivity",   0,    "Calendar with natural-language scheduling.",       "📅", 4.5, 16001, "com.mariner.tide",   "5.0.0", 60),
    App("app.041", "Forge VPN",            "Forge & Quill",      "Utilities",      599,  "Lightweight WireGuard VPN.",                       "🛡️", 4.6, 4300,  "com.forge.vpn",      "2.0.0", 22),
    App("app.042", "Beacon Health",        "Lumen Health",       "Medical",        0,    "Track meds, vitals, and appointments.",            "❤️", 4.4, 6800,  "com.lumen.beacon",   "3.1.0", 80),
    App("app.043", "Plain Text",           "Helio Tools",        "Developer Tools",0,    "Minimalist text editor with hotkeys.",             "🪶", 4.6, 8200,  "com.helio.plaintext","1.5.0", 12),
    App("app.044", "Glow Up",              "Tidepool Co.",       "Lifestyle",      199,  "Skincare routine reminder + tracker.",             "✨", 4.3, 2300,  "com.tidepool.glowup","1.0.0", 40),
    App("app.045", "Anvil Builds",         "Helio Tools",        "Developer Tools",999,  "CI dashboard for indie iOS devs.",                 "🔨", 4.5, 410,   "com.helio.anvil",    "2.1.0", 110),
    App("app.046", "Marisol Music",        "Octave Forge",       "Music",          0,    "Discover music from coastal cultures.",            "🎶", 4.6, 5300,  "com.octave.marisol", "1.4.0", 80),
    App("app.047", "Trove",                "Forge & Quill",      "Finance",        499,  "Subscription tracking + price alerts.",            "💰", 4.5, 7200,  "com.forge.trove",    "2.0.0", 35),
    App("app.048", "Birdsong",             "Tidepool Co.",       "Education",      299,  "Identify birds by their calls.",                   "🐦", 4.7, 6100,  "com.tidepool.birdsong","2.0.0",100),
    App("app.049", "Folio Reader",         "Mariner Inc.",       "Books",          499,  "Pro reader for epubs and PDFs with annotations.",  "📂", 4.5, 3400,  "com.mariner.folio",  "3.0.0", 70),
    App("app.050", "Mosaic Photos",        "Aurora Optics",      "Photo & Video",  0,    "AI-organized photo library.",                      "🖼️", 4.4, 19800, "com.aurora.mosaic",  "4.0.1", 110),
]


CATEGORIES = sorted({a.category for a in SEED_APPS})


def top_charts(limit: int = 10):
    """Return apps ordered by `rating_count` (a stand-in for popularity)."""
    return sorted(SEED_APPS, key=lambda a: a.rating_count, reverse=True)[:limit]
