import os
import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timezone
from supabase import create_client


# 1. SCRAPE PRICECHARTING

URL = "https://www.pricecharting.com/console/playstation-5"
HEADERS = {"User-Agent": "Mozilla/5.0 (Business IT school project)"}

response = requests.get(URL, headers=HEADERS, timeout=20)
response.raise_for_status()
response.encoding = "utf-8"

soup = BeautifulSoup(response.text, "html.parser")
scrape_time = datetime.now(timezone.utc).isoformat(timespec="seconds")

rows = []

for item in soup.select("#games_table tbody tr[data-product]"):
    title_el = item.select_one("td.title a")
    loose_el = item.select_one("td.used_price .js-price")
    cib_el = item.select_one("td.cib_price .js-price")
    new_el = item.select_one("td.new_price .js-price")

    title = title_el.get_text(" ", strip=True) if title_el else None
    product_url = (
        urljoin(URL, title_el.get("href"))
        if title_el and title_el.get("href")
        else None
    )

    rows.append({
        "title": title,
        "loose_price": loose_el.get_text(strip=True) if loose_el else None,
        "cib_price": cib_el.get_text(strip=True) if cib_el else None,
        "new_price": new_el.get_text(strip=True) if new_el else None,
        "product_url": product_url,
        "scraped_at": scrape_time,
    })

df = pd.DataFrame(rows)
print(f"Scraped {len(df)} raw rows")

if df.empty:
    raise RuntimeError("No products were scraped. The website structure may have changed.")


# --------------------------------------------------------------------------------------------------------------------
# 2. CLEAN DATA

for col in ["loose_price", "cib_price", "new_price"]:
    df[col] = (
        df[col]
        .astype("string")
        .str.replace(r"[^0-9.]", "", regex=True)
    )
    df[col] = pd.to_numeric(df[col], errors="coerce")

df = df.dropna(subset=["title"]).copy()
df = df.drop_duplicates(subset=["product_url"])

# Remove apostrophe variants that previously caused issues in the dashboard.
df["title"] = (
    df["title"]
    .str.replace("'", "", regex=False)
    .str.replace("’", "", regex=False)
    .str.replace("‘", "", regex=False)
)

# The source can include PlayStation hardware alongside games.
# The assignment dashboard analyses games only.
df = df[
    ~df["title"].str.contains("playstation", case=False, na=False)
].copy()


# --------------------------------------------------------------------------------------------------------------------
# 3. ENRICH DATA

df["new_vs_loose_diff"] = df["new_price"] - df["loose_price"]

df["new_vs_loose_pct"] = (
    (df["new_price"] - df["loose_price"])
    / df["loose_price"]
    * 100
).round(2)

# Avoid infinite percentage values when loose_price is 0.
df.loc[df["loose_price"] == 0, "new_vs_loose_pct"] = pd.NA

print(f"{len(df)} cleaned PS5 game rows ready for upload")


# ---------------------------------------------------------------------------------------
# 4. CONNECT TO SUPABASE

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------------------
# 5. PREPARE DATABASE ROWS

upload_rows = []

for _, row in df.iterrows():
    upload_rows.append({
        "title": str(row["title"]),
        "loose_price": None if pd.isna(row["loose_price"]) else float(row["loose_price"]),
        "cib_price": None if pd.isna(row["cib_price"]) else float(row["cib_price"]),
        "new_price": None if pd.isna(row["new_price"]) else float(row["new_price"]),
        "product_url": None if pd.isna(row["product_url"]) else str(row["product_url"]),
        "scraped_at": str(row["scraped_at"]),
        "new_vs_loose_diff": (
            None if pd.isna(row["new_vs_loose_diff"])
            else float(row["new_vs_loose_diff"])
        ),
        "new_vs_loose_pct": (
            None if pd.isna(row["new_vs_loose_pct"])
            else float(row["new_vs_loose_pct"])
        ),
    })

# ---------------------------------------------------------------------------------------
# 6. APPEND TO SUPABASE

if not upload_rows:
    raise RuntimeError("No cleaned rows are available to upload.")

supabase.table("ps5_prices").insert(upload_rows).execute()

print(
    f"Successfully inserted {len(upload_rows)} rows into "
    f"Supabase at {scrape_time}"
)

# ============================================================
# BUSINESS ALERT
# Alert when the average NEW price changes by more than 10%
# compared with the previous scrape
# ============================================================

try:
    # Average price of the current scrape
    current_avg = df["new_price"].mean()

    # Get historical rows from Supabase
    response = (
        supabase.table("ps5_prices")
        .select("new_price, scraped_at")
        .order("scraped_at", desc=True)
        .execute()
    )

    historical = pd.DataFrame(response.data)

    if not historical.empty:
        historical["scraped_at"] = pd.to_datetime(historical["scraped_at"],format="mixed",utc=True)

        # Find the different scrape moments
        scrape_times = sorted(
            historical["scraped_at"].dropna().unique(),
            reverse=True
        )

        # We need at least 2 scrape runs to compare
        if len(scrape_times) >= 2:
            previous_time = scrape_times[1]

            previous_rows = historical[
                historical["scraped_at"] == previous_time
            ]

            previous_avg = previous_rows["new_price"].mean()

            # Calculate percentage change
            change_pct = (
                (current_avg - previous_avg) / previous_avg
            ) * 100

            print(f"Current average new price: €{current_avg:.2f}")
            print(f"Previous average new price: €{previous_avg:.2f}")
            print(f"Price change: {change_pct:+.2f}%")

            # Business alert
            if abs(change_pct) >= 10:
                print(
                    f"⚠️ BUSINESS ALERT: Average PS5 game price "
                    f"changed by {change_pct:+.2f}%!"
                )
            else:
                print("✅ No alert: price change is below 10%.")

        else:
            print("Not enough historical scrapes for price comparison yet.")

except Exception as e:
    print(f"Could not calculate business alert: {e}")
