"""Starting data loaded into the database on every app start.

Seeding is idempotent: budget targets you've edited and merchant patterns
added by a user (added_by='user') are never overwritten.
"""

# Monthly variable-spending targets (USD).
BUDGET_TARGETS = {
    "Food & Dining": 800,
    "Travel": 1000,
    "Groceries": 800,
    "Health & Wellness": 450,
    "Taxes": 1000,  # set-aside for the year-end tax bill, not pure savings
    "Transport": 350,
    "Bills & Utilities": 215,
    "Shopping": 500,
    "Entertainment": 150,
    "Personal": 100,
}

# Two special categories that drive the is_oneoff / is_fixed flags.
ONEOFF = "One-off"
FIXED = "Fixed"

# category -> substrings to look for in the transaction description.
# Matching is case-insensitive and ignores spacing around '*'; when several patterns
# match, the LONGEST one wins (so "AMZN DIGITAL" beats "AMZN").
MERCHANT_MAP = {
    "Food & Dining": [
        "UBER *EATS", "DLO*UBER EATS", "DLO UBEREATS", "UBEREATS", "RAPPI",
        "DLO*RAPPI", "CASA NAGI", "EKILORE", "EL TIGRE", "GIORNALE",
        "BOSTON BAKERY", "JOE VENTURES", "LU PARTNERS", "TST*", "MAMAN",
        "CHICK-FIL", "MCDONALD", "MC DONALD", "BURGER", "STARBUCKS", "DOORDASH",
    ],
    "Shopping": [
        "AMAZON", "AMZN", "WALMART.COM", "TARGET T-", "TEMU", "NORDSTROM",
        "ZARA", "HUGO BOSS", "OLD NAVY", "MADEWELL", "BRFACTORY", "CARTERS",
        "BAMBIBABY", "INTER MIAMI CF RETAIL", "LEVI", "J.CREW", "VICTORIA",
        "H&M", "DECATHLON", "DEEPSTASH",
    ],
    "Travel": [
        "AMERICAN AI", "LATAM AIRLIN", "TACAINTL", "UNITED AIR", "MARRIOTT",
        "FOUR SEASONS", "FOUR POINTS", "SHERATON", "AIRBNB",
    ],
    "Transport": [
        "UBER *TRIP", "DLO*UBER RIDES", "UBR FLEX", "DLO*UBER RIDE", "LYFT",
        "SHELL", "CHEVRON", "MARATHON", "GASOL", "EXXON", "SUNPASS",
        "MPA PARKING", "SPOTHERO", "MIDTOWN MIAMI CDD", "PAYBYPHONE",
        "CORAL GABLES PAY",
    ],
    "Groceries": [
        "SUMESA", "PUBLIX", "ALDI", "WHOLEFDS", "TRADER JOE", "OXXO",
        "WALMART",  # in-store; WALMART.COM (longer) wins for online orders
        "PRICE CHOICE", "7 ELEVEN", "NATURAL POLANCO", "CK LIEJA",
        "MERPAGO*ANEC", "MERPAGO*DELI", "EST NATURAL",
    ],
    "Health & Wellness": [
        "UHEALTH", "MSMC", "NICKLAUS", "QUEST DIAG", "CVS", "WALGREEN",
        "FARMATO", "CLINICA", "AWAKE ANIMAL", "DRA YOLANDA", "CLIP MX*AMAM",
        "AMAMANTANDO", "SMARTFIT", "CONEKTA*SICLO", "ZTL*SICLO",
        "ACUATIC",  # Mateo's swim lessons
    ],
    "Entertainment": [
        "CINEPOLIS", "ZOO DE CHAP", "PINECREST", "FROST SCIENCE",
        "KIDS EMPIRE", "JUNGLE ISLAND",
    ],
    "Bills & Utilities": [
        "SPOTIFY", "NETFLIX", "APPLE.COM/BILL", "ANTHROPIC", "CLAUDE",
        "PRIME VIDEO", "AMZN DIGITAL",
    ],
    "Personal": [
        "BARBA NEGRA", "THE BAR BARBER", "NAILS", "SALON", "HARMONY SALON",
        "BONBONITE", "CHILDRENSALON", "FRESKO SAL", "SAL CONFIDENCE",
        "GO NAILS",
        "SHANNON-USHER",  # recurring coaching
        "VAGARO",
    ],
    ONEOFF: [
        "ARPIN", "PUBLIC STORAGE", "USCIS", "INM RECAUDA", "NIC*-FL SUNBIZ",
        "USCUSTOMS",
    ],
    FIXED: [
        "AUNA",  # parents' insurance + Maura care
        "TOTALPLAY",  # internet
    ],
}

# Merchants that keep their normal category but become one-offs above a
# dollar threshold, e.g. a big lab bill vs. a routine test.
CONDITIONAL_ONEOFFS = {
    "QUEST DIAG": 100,
}
