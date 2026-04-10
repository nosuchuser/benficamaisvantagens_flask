"""
Benfica Mais Vantagens – Web App (v12)
======================================
Serves a responsive single-page explorer for the partner database.
Refactored with improved structure, logging, caching, and map-based filtering.
Enhanced with auto-zoom to fit all pins and map location search overlay.

Usage:
    pip install flask
    python site.py [--db benfica_parceiros.db] [--port 5000] [--host 127.0.0.1]
"""

import argparse
import os
import json
import logging
import sqlite3
from functools import lru_cache
from pathlib import Path
from contextlib import contextmanager

from flask import Flask, jsonify, request, send_from_directory, Response

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_DB   = "benfica_parceiros.db"
DEFAULT_PORT = 5000

# Estádio da Luz coordinates
STADIUM_LAT = 38.75271399047523
STADIUM_LNG = -9.184760992906085

# About modal customisation
ABOUT_EMAIL      = "youremail@email.com"
ABOUT_KOFI       = "https://ko-fi.com/beg_some_kofi"
ABOUT_CLAUDE_URL = "https://claude.ai"

# Logger
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

app     = Flask(__name__)

@app.before_request
def log_request_info():
    # Proxies often send a comma-separated list of IPs. The first is usually the real client.
    forwarded_for = request.headers.get('X-Forwarded-For')
    client_ip = forwarded_for.split(',')[0].strip() if forwarded_for else request.remote_addr

DB_PATH = DEFAULT_DB   # overridden at startup by --db

# ─── DB helper ────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Context manager for database connections."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _validate_db_schema():
    """Validate database schema at startup. Fail fast if missing."""
    with get_db() as con:
        try:
            con.execute("SELECT 1 FROM partners LIMIT 1").fetchone()
            logger.info(f"✓ Database schema validated: {DB_PATH}")
        except sqlite3.OperationalError as e:
            logger.error(f"✗ Database schema invalid: {e}")
            raise


def _fetch_all_partners():
    """Fetch all partners from DB. Cached for performance."""
    with get_db() as con:
        try:
            # Try new schema first (with discounts_json / partner_url)
            rows = con.execute(
                "SELECT name, category, discount, discounts_json, partner_url, website, "
                "       location, latitude, longitude, "
                "       added_at, last_modified, crawled_at "
                "FROM partners ORDER BY name, location"
            ).fetchall()
        except sqlite3.OperationalError:
            # Fall back to older schema
            rows = con.execute(
                "SELECT name, category, discount, website, "
                "       location, latitude, longitude, "
                "       added_at, last_modified, crawled_at "
                "FROM partners ORDER BY name, location"
            ).fetchall()

    partners = {}
    for r in rows:
        key = r["name"]
        if key not in partners:
            raw_dj = dict(r).get("discounts_json") or "[]"
            partners[key] = {
                "name":          r["name"],
                "category":      r["category"]     or "",
                "discount":      r["discount"]      or "",
                "discounts":     json.loads(raw_dj),
                "partner_url":   dict(r).get("partner_url") or "",
                "website":       r["website"]       or "",
                "added_at":      r["added_at"]      or "",
                "last_modified": r["last_modified"] or "",
                "crawled_at":    r["crawled_at"]    or "",
                "locations":     [],
            }
        if r["location"]:
            partners[key]["locations"].append({
                "name": r["location"],
                "lat":  r["latitude"],
                "lng":  r["longitude"],
            })

    return list(partners.values())


# ─── Geolocation helpers ──────────────────────────────────────────────────────

def haversine(lat1, lng1, lat2, lng2):
    """Calculate distance in km between two coordinates."""
    if lat2 is None or lng2 is None:
        return float('inf')

    import math
    R = 6371  # Earth radius in km
    to_rad = lambda x: x * math.pi / 180

    dlat = to_rad(lat2 - lat1)
    dlng = to_rad(lng2 - lng1)
    a = (math.sin(dlat/2)**2 +
         math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(dlng/2)**2)
    return R * 2 * math.asin(math.sqrt(a))


def get_nearest_locations(partners, lat, lng, limit=100):
    """Get all locations from partners, sorted by distance."""
    locations = []
    for p in partners:
        for loc in p["locations"]:
            if loc["lat"] is not None and loc["lng"] is not None:
                distance = haversine(lat, lng, loc["lat"], loc["lng"])
                locations.append({
                    "partner_name": p["name"],
                    "partner_category": p["category"],
                    "partner_discount": p["discount"],
                    "partner_url": p["partner_url"],
                    "partner_website": p["website"],
                    "location_name": loc["name"],
                    "lat": loc["lat"],
                    "lng": loc["lng"],
                    "distance": distance,
                })

    locations.sort(key=lambda x: x["distance"])
    return locations[:limit]


# ─── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/partners")
def api_partners():
    """List all partners with optional search and category filtering."""
    search     = (request.args.get("search", "") or "").strip().lower()
    cats_param = (request.args.get("categories", "") or "").strip()
    cat_filter = {c.strip() for c in cats_param.split(",") if c.strip()}

    partners = _fetch_all_partners()

    result = partners
    if search:
        result = [p for p in result if search in p["name"].lower()]
    if cat_filter:
        result = [p for p in result if p["category"] in cat_filter]

    logger.info(f"Partners query: search='{search}', cats={cat_filter}, results={len(result)}")
    return jsonify({"partners": result, "total": len(result)})


@app.route("/api/categories")
def api_categories():
    """List all unique partner categories."""
    with get_db() as con:
        try:
            rows = con.execute(
                "SELECT DISTINCT category FROM partners "
                "WHERE category IS NOT NULL AND category != '' ORDER BY category"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    categories = [r["category"] for r in rows]
    logger.info(f"Categories query: {len(categories)} categories")
    return jsonify({"categories": categories})


@app.route("/api/stats")
def api_stats():
    """Return database statistics."""
    with get_db() as con:
        try:
            total_partners   = con.execute("SELECT COUNT(DISTINCT name) FROM partners").fetchone()[0]
            total_locations  = con.execute("SELECT COUNT(*) FROM partners WHERE location != ''").fetchone()[0]
            total_coords     = con.execute("SELECT COUNT(*) FROM partners WHERE latitude IS NOT NULL").fetchone()[0]
            crawled_at       = con.execute("SELECT MAX(crawled_at) FROM partners").fetchone()[0]
            new_7d           = con.execute(
                "SELECT COUNT(DISTINCT name) FROM partners WHERE added_at >= datetime('now','-7 days')"
            ).fetchone()[0]
            upd_7d           = con.execute(
                "SELECT COUNT(DISTINCT name) FROM partners "
                "WHERE last_modified IS NOT NULL AND last_modified >= datetime('now','-7 days')"
            ).fetchone()[0]
            backups          = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'partners_backup_%' "
                "ORDER BY name DESC LIMIT 5"
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.error(f"Error fetching stats: {e}")
            return jsonify({"error": "Stats unavailable"}), 500

    return jsonify({
        "total_partners":  total_partners,
        "total_locations": total_locations,
        "total_coords":    total_coords,
        "crawled_at":      crawled_at,
        "new_last_7d":     new_7d,
        "updated_last_7d": upd_7d,
        "recent_backups":  [r["name"] for r in backups],
    })


@app.route("/api/map/search")
def api_map_search():
    """
    Search partners for map display. Query params:
    - search: Partner name filter
    - categories: Comma-separated category filter
    - lat, lng: Map center (for 'search here' feature)
    - limit: Max results (default 100)
    """
    search     = (request.args.get("search", "") or "").strip().lower()
    cats_param = (request.args.get("categories", "") or "").strip()
    lat_str    = request.args.get("lat", "").strip()
    lng_str    = request.args.get("lng", "").strip()
    limit_str  = request.args.get("limit", "100").strip()

    cat_filter = {c.strip() for c in cats_param.split(",") if c.strip()}

    try:
        limit = int(limit_str)
    except ValueError:
        limit = 100

    partners = _fetch_all_partners()

    # Apply filters
    result = partners
    if search:
        result = [p for p in result if search in p["name"].lower()]
    if cat_filter:
        result = [p for p in result if p["category"] in cat_filter]

    # If center coordinates provided, sort by distance
    has_center = False
    try:
        center_lat = float(lat_str)
        center_lng = float(lng_str)
        has_center = True
    except (ValueError, TypeError):
        pass

    if has_center:
        # Get all locations from filtered partners, sorted by distance
        locations = get_nearest_locations(result, center_lat, center_lng, limit=limit)
        logger.info(f"Map search: center=({center_lat:.4f},{center_lng:.4f}), "
                   f"filters=search:'{search}' cats:{cat_filter}, results={len(locations)}")
        return jsonify({
            "type": "locations",
            "center": {"lat": center_lat, "lng": center_lng},
            "locations": locations,
            "total": len(locations),
        })
    else:
        # No center: return first N partners with all their locations
        locations = []
        for p in result[:limit]:
            for loc in p["locations"]:
                if loc["lat"] is not None and loc["lng"] is not None:
                    locations.append({
                        "partner_name": p["name"],
                        "partner_category": p["category"],
                        "partner_discount": p["discount"],
                        "partner_url": p["partner_url"],
                        "partner_website": p["website"],
                        "location_name": loc["name"],
                        "lat": loc["lat"],
                        "lng": loc["lng"],
                        "distance": None,
                    })
        logger.info(f"Map search (no center): filters=search:'{search}' cats:{cat_filter}, results={len(locations)}")
        return jsonify({
            "type": "locations",
            "center": None,
            "locations": locations,
            "total": len(locations),
        })


# ─── Frontend ─────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, minimum-scale=1.0, maximum-scale=1.0, user-scalable=no"/>
<title>+Vantagens</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
:root{
  --red:#D4002A;--dark:#1a1a1a;--light:#f4f4f4;--card:#fff;
  --muted:#666;--border:#e0e0e0;--radius:10px;--shadow:0 2px 8px rgba(0,0,0,.08);
}
*{box-sizing:border-box;margin:0;padding:0}
html{height:100%;min-height:100vh}
body{height:100%;min-height:100vh;display:flex;flex-direction:column;
     font-family:'Segoe UI',system-ui,sans-serif;
     background:var(--light);color:var(--dark);overflow:hidden}

/* ─── Header ─── */
header{flex-shrink:0;background:var(--red);color:#fff;
       padding:0 14px;height:52px;display:flex;align-items:center;
       gap:10px;z-index:200;box-shadow:0 2px 8px rgba(0,0,0,.3)}
.logo{font-size:1.1rem;font-weight:700;white-space:nowrap;flex-shrink:0}
.logo em{font-style:normal;opacity:.7;font-weight:400;font-size:.85rem}
#searchWrap{flex:1;min-width:80px;position:relative}
#searchInput{width:100%;padding:7px 10px 7px 30px;border:none;border-radius:20px;
             font-size:.87rem;outline:none;
             background:rgba(255,255,255,.18);color:#fff}
#searchInput::placeholder{color:rgba(255,255,255,.55)}
#searchInput:focus{background:rgba(255,255,255,.28)}
.si{position:absolute;left:9px;top:50%;transform:translateY(-50%);
    opacity:.6;font-size:.82rem;pointer-events:none}
#aboutBtn{background:none;border:none;color:#fff;font-size:1.15rem;
          cursor:pointer;opacity:.8;padding:4px;flex-shrink:0}
#aboutBtn:hover{opacity:1}

/* ─── Controls ─── */
#ctrl{flex-shrink:0;background:#fff;border-bottom:1px solid var(--border);
      padding:6px 12px;display:flex;flex-direction:column;gap:5px;z-index:100}
#ctrl1{display:flex;align-items:center;gap:6px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin}
#ctrl1::-webkit-scrollbar{height:3px}
#ctrl1::-webkit-scrollbar-track{background:transparent}
#ctrl1::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
#nearBtn{background:var(--red);color:#fff;border:none;border-radius:20px;
         padding:5px 11px;font-size:.79rem;cursor:pointer;
         white-space:nowrap;flex-shrink:0;transition:opacity .2s}
#nearBtn:hover{opacity:.85}
#nearBtn.on{background:#1a7f37}
#clearBtn{display:none;background:none;border:1px solid var(--border);
          border-radius:20px;padding:4px 9px;font-size:.76rem;
          cursor:pointer;color:var(--muted);flex-shrink:0}
.spacer{flex:1}
.vbtns{display:flex;border:1px solid var(--border);border-radius:6px;
       overflow:hidden;flex-shrink:0}
.vbtn{border:none;background:#fff;padding:5px 10px;font-size:.77rem;
      cursor:pointer;transition:background .15s}
.vbtn.on{background:var(--red);color:#fff}
#sortSel{border:1px solid var(--border);border-radius:6px;padding:4px 6px;
         font-size:.77rem;background:#fff;cursor:pointer;
         flex-shrink:0;max-width:130px}
#cats{display:flex;gap:5px;overflow-x:auto;padding-bottom:1px;
      scrollbar-width:none;flex-wrap:nowrap;min-height:32px}
#cats::-webkit-scrollbar{display:none}
.pill{border:1.5px solid var(--border);background:#fff;border-radius:20px;
      padding:5px 13px;font-size:.77rem;cursor:pointer;white-space:nowrap;
      transition:all .15s;user-select:none;flex-shrink:0}
.pill.on{background:var(--red);border-color:var(--red);color:#fff}

/* ─── Meta bar ─── */
#meta{flex-shrink:0;padding:4px 14px;font-size:.76rem;color:var(--muted);
      display:flex;justify-content:space-between;gap:8px}
#geoTxt{color:#1a7f37;font-size:.73rem}

/* ─── Content area ─── */
#content{flex:1;min-height:0;position:relative}

/* List */
#listView{position:absolute;inset:0;overflow-y:auto;padding:10px 12px 20px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}

/* Cards */
.card{background:var(--card);border-radius:var(--radius);
      box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}
.card:hover{box-shadow:0 4px 16px rgba(0,0,0,.13)}
.ch{background:var(--red);padding:10px 12px;display:flex;
    align-items:flex-start;justify-content:space-between;gap:8px}
.cname{color:#fff;font-weight:700;font-size:.94rem;line-height:1.3}
.cname a{color:inherit;text-decoration:none}
.cname a:hover{text-decoration:underline}
.badges{display:flex;flex-direction:column;gap:3px;align-items:flex-end;flex-shrink:0}
.bcat{background:rgba(255,255,255,.22);color:#fff;border-radius:10px;
      padding:2px 7px;font-size:.67rem;white-space:nowrap}
.bnew{background:#1a7f37;color:#fff;border-radius:10px;
      padding:2px 7px;font-size:.67rem;white-space:nowrap}
.bupd{background:#c47900;color:#fff;border-radius:10px;
      padding:2px 7px;font-size:.67rem;white-space:nowrap}
.cb{padding:10px 12px;flex:1;display:flex;flex-direction:column;gap:6px}
.disc{font-weight:700;font-size:.89rem;color:var(--red)}
.disc a{color:inherit;text-decoration:none}
.disc a:hover{text-decoration:underline}
.disc-det{font-size:.72rem;color:var(--red);opacity:.75;margin-left:4px;
          cursor:pointer;text-decoration:underline}
.disc-extra{display:none;margin-top:3px;padding:5px 8px;
            background:var(--light);border-radius:6px;
            font-size:.76rem;color:var(--dark)}
.disc-extra li{margin-left:14px;margin-bottom:2px}
.ddesc{font-size:.75rem;color:var(--muted);margin-top:-3px}
.locs{display:flex;flex-direction:column;gap:3px;margin-top:2px}
.lr{display:flex;align-items:center;gap:5px;font-size:.78rem}
.lpin{color:var(--red);flex-shrink:0}
.lname{flex:1}
.lname a{color:inherit;text-decoration:none}
.lname a:hover{text-decoration:underline}
.ldist{color:#1a7f37;font-weight:600;white-space:nowrap;font-size:.72rem}
.more{font-size:.72rem;color:var(--muted);cursor:pointer;
      text-decoration:underline;margin-top:2px;display:inline-block}
.cf{padding:6px 12px;border-top:1px solid var(--border);display:flex;justify-content:flex-end}
.slink{font-size:.75rem;color:var(--red);text-decoration:none;font-weight:600}
.slink:hover{text-decoration:underline}
.nores{text-align:center;color:var(--muted);padding:60px 20px;grid-column:1/-1}
.nores h2{font-size:1.4rem;margin-bottom:8px}

/* Map */
#mapView{position:absolute;inset:0;display:none}
#map{width:100%;height:100%}
#mapDbg{position:absolute;bottom:8px;left:8px;
        background:rgba(0,0,0,.62);color:#fff;font-size:.68rem;
        padding:5px 9px;border-radius:6px;z-index:1000;
        pointer-events:none;max-width:300px;white-space:pre-wrap;display:none}

/* Map search overlay */
#mapSearchOverlay{position:absolute;top:8px;left:48px;
                  background:#fff;border-radius:6px;
                  box-shadow:0 2px 8px rgba(0,0,0,.15);z-index:1001;
                  display:flex;flex-direction:column;width:160px;
                  height:auto;overflow:hidden}
#mapSearchInput{width:100%;padding:6px 8px;border:none;border-radius:6px 6px 0 0;
                font-size:.68rem;outline:none;
                border-bottom:1px solid var(--border);flex-shrink:0}
#mapSearchInput::placeholder{color:var(--muted)}
#mapSearchInput:focus{outline:2px solid var(--red);outline-offset:-2px}
#mapSearchResults{max-height:0;overflow-y:auto;list-style:none;
                  transition:max-height .25s ease;flex:1}
#mapSearchResults.open{max-height:200px}
#mapSearchResults li{padding:4px 6px;border-bottom:1px solid var(--border);
                     font-size:.7rem;cursor:pointer;transition:background .1s}
#mapSearchResults li:hover{background:var(--light)}
#mapSearchResults li:last-child{border-bottom:none}
#mapSearchResults li strong{display:block;font-size:.72rem;font-weight:600}
#mapSearchResults li small{display:block;font-size:.65rem;color:var(--muted);margin-top:1px}
#mapSearchResults.empty{padding:6px;color:var(--muted);font-size:.7rem;
                        text-align:center}

#mapSearchBtn{position:absolute;top:10px;right:10px;
              background:var(--red);color:#fff;border:none;border-radius:6px;
              padding:8px 14px;font-size:.79rem;cursor:pointer;
              z-index:1001;display:none;transition:opacity .2s}
#mapSearchBtn:hover{opacity:.85}
#mapEmpty{position:absolute;inset:0;display:none;
          align-items:center;justify-content:center;
          background:rgba(0,0,0,.05);flex-direction:column;gap:12px;
          color:var(--muted);z-index:999}
#mapEmpty h2{font-size:1.3rem}
#mapEmpty p{font-size:.85rem;max-width:300px;text-align:center}

/* New max limit map message */
#mapMaxMsg{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);
           background:rgba(0,0,0,.75);color:#fff;padding:6px 14px;
           border-radius:20px;font-size:.8rem;z-index:1000;
           display:none;pointer-events:none;white-space:nowrap;}

/* Loading */
#loading{position:fixed;inset:0;background:rgba(255,255,255,.93);
         display:flex;align-items:center;justify-content:center;
         z-index:9999;flex-direction:column;gap:12px}
.spin{width:36px;height:36px;border:4px solid var(--border);
      border-top-color:var(--red);border-radius:50%;
      animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* About modal */
#aov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.52);
     z-index:10000;align-items:center;justify-content:center}
#aov.open{display:flex}
#abox{background:#fff;border-radius:14px;padding:26px 22px;
      max-width:360px;width:90%;position:relative;
      box-shadow:0 8px 32px rgba(0,0,0,.2)}
#abox h2{font-size:1.05rem;margin-bottom:12px;color:var(--red)}
#abox p{font-size:.86rem;line-height:1.6;margin-bottom:9px}
#abox a{color:var(--red);text-decoration:none;font-weight:600}
#abox a:hover{text-decoration:underline}
#acl{position:absolute;right:12px;top:12px;border:none;background:none;
     font-size:1.15rem;cursor:pointer;color:var(--muted);line-height:1}

/* Mobile */
@media(max-width:480px){
  header{height:46px;padding:0 10px;gap:7px}
  .logo{font-size:.98rem}
  #ctrl{padding:5px 9px;gap:4px}
  #sortSel{max-width:90px;font-size:.72rem}
  .vbtn{padding:4px 7px;font-size:.71rem}
  #nearBtn{padding:4px 9px;font-size:.74rem}
  #grid{grid-template-columns:1fr}
  #mapSearchBtn{padding:6px 11px;font-size:.74rem}
  #mapSearchOverlay{width:calc(100vw - 56px);max-width:180px;left:48px}
}

/* Ultra-tall devices (iPhone Pro Max and similar) */
@media(max-height:700px){
  header{height:44px !important;padding:0 8px !important}
  #ctrl{padding:3px 6px !important;gap:3px !important}
  #ctrl1{gap:3px !important;font-size:.7rem}
  #nearBtn{padding:2px 6px;font-size:.7rem;min-width:auto}
  .vbtn{padding:2px 4px;font-size:.65rem}
  #sortSel{padding:2px 4px;font-size:.68rem}
  #meta{padding:1px 10px;font-size:.68rem;gap:4px}
  .pill{padding:3px 8px;font-size:.68rem}
  #cats{gap:3px;min-height:26px}
  .card{border-radius:6px;padding:0}
  .ch{padding:6px 8px}
  .cname{font-size:.85rem}
  .cb{padding:6px 8px;gap:3px}
  .disc{font-size:.8rem}
  .locs{margin-top:1px}
  .lr{font-size:.7rem;gap:3px}
}
</style>
</head>
<body>

<div id="loading"><div class="spin"></div><div>A carregar…</div></div>

<div id="aov">
  <div id="abox">
    <button id="acl" aria-label="Fechar">✕</button>
    <h2>💡 This is the "about" page</h2>
    Send email to <a href="mailto:{{ about_email }}">{{ about_email }}</a></p>
    <p>Send <a href="{{ about_kofi }}" target="_blank" rel="noopener">Ko-fi</a></p><p>
    <p>Vibe coded with <a href="{{ about_claude_url }}" target="_blank" rel="noopener">Claude AI</a> ✨</p>
  </div>
</div>

<header>
  <div class="logo">🦅 <b>+</b>Vantagens</div>
  <div id="searchWrap">
    <span class="si">🔍</span>
    <input id="searchInput" type="search" placeholder="Procura parceiro…" autocomplete="off"/>
  </div>
  <button id="aboutBtn" title="Sobre">💡</button>
</header>

<div id="ctrl">
  <div id="ctrl1">
    <button id="nearBtn">📍 +Proximo</button>
    <button id="clearBtn" title="Limpar">✕</button>
    <div class="spacer"></div>
    <div class="vbtns">
      <button class="vbtn on" id="listBtn">📋 Lista</button>
      <button class="vbtn"    id="mapBtn" >🗺 Mapa</button>
    </div>
    <select id="sortSel">
      <option value="name">Ord: Alfabética</option>
      <option value="dist">Ord: Proximidade.</option>
      <option value="cat" >Ord: Categoria</option>
      <option value="add" >Ord: Novos</option>
      <option value="mod" >Ord: Atualizados</option>
    </select>
  </div>
  <div id="cats"></div>
</div>

<div id="meta">
  <span id="cnt"></span>
  <span id="geoTxt"></span>
</div>

<div id="content">
  <div id="listView"><div id="grid"></div></div>
  <div id="mapView">
    <div id="map"></div>
    <div id="mapEmpty"></div>
    <div id="mapDbg"></div>
    <div id="mapSearchOverlay">
      <input id="mapSearchInput" type="text" placeholder="procurar morada/localidade" autocomplete="off"/>
      <ul id="mapSearchResults"></ul>
    </div>
    <button id="mapSearchBtn">Refrescar parceiros</button>
    <div id="mapMaxMsg"></div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
'use strict';

// ─── State ────────────────────────────────────────────────────────────────────
const MAX_PINS = 200;
const STADIUM = { lat: 38.75271399047523, lng: -9.184760992906085 };

const STATE = {
  all: [],
  cats: [],
  activeCats: new Set(),
  searchQuery: '',
  userLat: null,  // Specifically for precise geolocation list view distance
  userLng: null,
  currentView: 'list',
  leafletMap: null,
  markers: [],
  mapInitialized: false,
  searchCenter: { lat: STADIUM.lat, lng: STADIUM.lng }, // Dynamic user/search coordinates
  mapLocationData: [], // Store last loaded locations for zoom fitting
  firstMapLoadAfterSwitch: false, // Flag to trigger auto-zoom on view switch
  isAutoZooming: false, // Flag to track if map zoom is auto-triggered
};

// ─── Geo maths ────────────────────────────────────────────────────────────────
function haversine(lat1, lng1, lat2, lng2) {
  if (lat2 == null || lng2 == null) return Infinity;
  const R = 6371, toRad = x => x * Math.PI / 180;
  const dlat = toRad(lat2 - lat1), dlng = toRad(lng2 - lng1);
  const a = Math.sin(dlat/2)**2 +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dlng/2)**2;
  return R * 2 * Math.asin(Math.sqrt(a));
}

function formatDistance(km) {
  return km === Infinity ?
    '' : (km < 1 ? Math.round(km * 1000) + ' m' : km.toFixed(1) + ' km');
}

function isRecent(dateStr, days = 7) {
  return dateStr && (Date.now() - new Date(dateStr).getTime()) < days * 86400000;
}

// ─── Escape HTML ──────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ─── Filter/sort ──────────────────────────────────────────────────────────────
function getVisiblePartners() {
  let result = STATE.all;
  if (STATE.searchQuery) {
    result = result.filter(p => p.name.toLowerCase().includes(STATE.searchQuery));
  }
  if (STATE.activeCats.size > 0) {
    result = result.filter(p => STATE.activeCats.has(p.category));
  }

  result = [...result];
  const sortBy = document.getElementById('sortSel').value;

  if (sortBy === 'name') {
    result.sort((a, b) => a.name.localeCompare(b.name));
  } else if (sortBy === 'cat') {
    result.sort((a, b) => (a.category || '').localeCompare(b.category || '') ||
                          a.name.localeCompare(b.name));
  } else if (sortBy === 'dist') {
    result.sort((a, b) => {
      const dA = getNearest(a).distance;
      const dB = getNearest(b).distance;
      return dA - dB;
    });
  } else if (sortBy === 'add') {
    result.sort((a, b) => (b.added_at || '').localeCompare(a.added_at || ''));
  } else if (sortBy === 'mod') {
    result.sort((a, b) => (b.last_modified || '').localeCompare(a.last_modified || ''));
  }

  return result;
}

function getNearest(partner) {
  if (STATE.userLat == null) {
    return { location: partner.locations[0] || null, distance: Infinity };
  }
  let best = null, bestDist = Infinity;
  partner.locations.forEach(loc => {
    const d = haversine(STATE.userLat, STATE.userLng, loc.lat, loc.lng);
    if (d < bestDist) { bestDist = d; best = loc; }
  });
  return { location: best, distance: bestDist };
}

// ─── Render list ──────────────────────────────────────────────────────────────
function renderList() {
  const grid = document.getElementById('grid');
  const items = getVisiblePartners();
  document.getElementById('cnt').textContent = items.length + ' de ' + STATE.all.length + ' parceiros';

  if (!items.length) {
    grid.innerHTML = '<div class="nores"><h2>Sem resultados</h2><p>Tenta outra pesquisa.</p></div>';
    return;
  }

  const hasGeo = STATE.userLat != null;
  grid.innerHTML = items.map(p => {
    const slbUrl = p.partner_url || '';
    const nameHtml = slbUrl
      ? `<a href="${escapeHtml(slbUrl)}" target="_blank" rel="noopener">${escapeHtml(p.name)}</a>`
      : escapeHtml(p.name);

    // Discounts
    const discs = (p.discounts && p.discounts.length) ? p.discounts : [p.discount || ''];
    const primary = discs[0] || '';
    const parts = primary.split(' – ');
    const dval = parts[0] || '';
    const ddesc = parts.slice(1).join(' – ');

    const discLink = slbUrl
      ? `<a href="${escapeHtml(slbUrl)}" target="_blank" rel="noopener">${escapeHtml(dval)}</a>`
      : escapeHtml(dval);

    const multiId = 'md-' + p.name.replace(/[^a-z0-9]/gi, '');
    const detailsBtn = discs.length > 1
      ? `<span class="disc-det" onclick="toggleDisc('${multiId}')">+detalhes</span>` : '';
    const detailsBox = discs.length > 1
      ? `<div class="disc-extra" id="${multiId}"><ul>${discs.map(d => '<li>' + escapeHtml(d) + '</li>').join('')}</ul></div>`
      : '';

    // Locations (nearest first when geo active)
    const sorted = hasGeo
      ? [...p.locations].sort((a, b) => haversine(STATE.userLat, STATE.userLng, a.lat, a.lng) -
                                        haversine(STATE.userLat, STATE.userLng, b.lat, b.lng))
      : p.locations;

    const SHOW = 3;
    const locHtml = sorted.slice(0, SHOW).map(l => {
      const d = hasGeo ? haversine(STATE.userLat, STATE.userLng, l.lat, l.lng) : Infinity;
      const mapsUrl = (l.lat != null && l.lng != null)
        ? `https://www.google.com/maps?q=${l.lat},${l.lng}` : '';
      const lnk = mapsUrl
        ? `<a href="${mapsUrl}" target="_blank" rel="noopener">${escapeHtml(l.name)}</a>`
        : escapeHtml(l.name);
      return `<div class="lr"><span class="lpin">📍</span><span class="lname">${lnk}</span>${d < Infinity ? `<span class="ldist">${formatDistance(d)}</span>` : ''}</div>`;
    }).join('');

    const extra = sorted.length - SHOW;
    const locsJson = JSON.stringify(sorted.slice(SHOW)).replace(/'/g, '&#39;');
    const moreHtml = extra > 0
      ? `<span class="more" data-locs='${locsJson}' data-hasgeo='${hasGeo}' onclick="showMore(this)">+${extra} mais</span>`
      : '';

    const isNew = isRecent(p.added_at, 7);
    const isUpd = !isNew && isRecent(p.last_modified, 7);

    return `<div class="card">
  <div class="ch">
    <div class="cname">${nameHtml}</div>
    <div class="badges">
      ${p.category ? `<span class="bcat">${escapeHtml(p.category)}</span>` : ''}
      ${isNew ? '<span class="bnew">✦ Novo</span>' : ''}
      ${isUpd ? '<span class="bupd">↻ Atualizado</span>' : ''}
    </div>
  </div>
  <div class="cb">
    ${dval ? `<div class="disc">${discLink}${detailsBtn}</div>${detailsBox}` : ''}
    ${ddesc ? `<div class="ddesc">${escapeHtml(ddesc)}</div>` : ''}
    ${sorted.length ? `<div class="locs">${locHtml}${moreHtml}</div>` : ''}
  </div>
  ${p.website ? `<div class="cf"><a class="slink" href="${escapeHtml(p.website)}" target="_blank" rel="noopener">🔗 Website ↗</a></div>` : ''}
</div>`;
  }).join('');
}

function toggleDisc(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'block' ? 'none' : 'block';
}

function showMore(el) {
  const locs = JSON.parse(el.getAttribute('data-locs'));
  const hasGeo = el.getAttribute('data-hasgeo') === 'true';
  locs.forEach(l => {
    const d = hasGeo && STATE.userLat != null ? haversine(STATE.userLat, STATE.userLng, l.lat, l.lng) : Infinity;
    const mapsUrl = (l.lat != null && l.lng != null) ? `https://www.google.com/maps?q=${l.lat},${l.lng}` : '';
    const lnk = mapsUrl ? `<a href="${mapsUrl}" target="_blank" rel="noopener">${escapeHtml(l.name)}</a>` : escapeHtml(l.name);
    const row = document.createElement('div');
    row.className = 'lr';
    row.innerHTML = `<span class="lpin">📍</span><span class="lname">${lnk}</span>${d < Infinity ? `<span class="ldist">${formatDistance(d)}</span>` : ''}`;
    el.parentElement.insertBefore(row, el);
  });
  el.remove();
}

// ─── Map – Auto-zoom to fit all pins (on view switch) ──────────────────────
function fitMapBounds() {
  if (!STATE.leafletMap || STATE.mapLocationData.length === 0) return;

  const bounds = L.latLngBounds([]);

  // Include user location if available
  if (STATE.userLat != null && STATE.userLng != null) {
    bounds.extend([STATE.userLat, STATE.userLng]);
  }

  // Include all partner locations
  STATE.mapLocationData.forEach(loc => {
    if (loc.lat != null && loc.lng != null) {
      bounds.extend([loc.lat, loc.lng]);
    }
  });

  // Fit map to bounds with slight padding
  if (bounds.isValid()) {
    STATE.leafletMap.fitBounds(bounds, { padding: [50, 50], maxZoom: 15 });
  }
}

// ─── Map location search ──────────────────────────────────────────────────────
// Nominatim search with debouncing
let nominatimTimeout;
function updateMapSearchResults() {
  const input = document.getElementById('mapSearchInput');
  const query = input.value.trim();
  const resultsList = document.getElementById('mapSearchResults');

  // Clear previous timeout
  clearTimeout(nominatimTimeout);

  if (!query || query.length < 2) {
    resultsList.innerHTML = '';
    resultsList.classList.remove('empty');
    resultsList.classList.remove('open');
    return;
  }

  // Show loading state
  resultsList.innerHTML = '<li class="empty">A procurar…</li>';
  resultsList.classList.add('open');

  // Debounce API calls
  nominatimTimeout = setTimeout(() => {
    const params = new URLSearchParams({
      q: query,
      countrycodes: 'pt',
      format: 'json',
      limit: 10,
      featuretype: 'settlement,street,amenity,landmark',
    });

    fetch(`https://nominatim.openstreetmap.org/search?${params}`, {
      headers: { 'Accept-Language': 'pt-PT' }
    })
      .then(r => r.json())
      .then(data => {
        if (!data || data.length === 0) {
          resultsList.innerHTML = '<li class="empty">Nenhum resultado</li>';
          resultsList.classList.add('empty');
          resultsList.classList.add('open');
          return;
        }

        resultsList.classList.remove('empty');
        resultsList.classList.add('open');
        resultsList.innerHTML = data.slice(0, 10).map(loc =>
          `<li onclick="zoomToNominatimLocation(${loc.lat}, ${loc.lon}, '${escapeHtml(loc.display_name)}')">
             <strong>${escapeHtml(loc.name || loc.display_name.split(',')[0])}</strong>
             <small>${escapeHtml(loc.display_name.substring(0, 60))}</small>
           </li>`
        ).join('');
      })
      .catch(err => {
        resultsList.innerHTML = '<li class="empty">Erro na busca</li>';
        resultsList.classList.add('empty');
        resultsList.classList.add('open');
      });
  }, 400); // 400ms debounce
}

function zoomToNominatimLocation(lat, lng, name) {
  if (!STATE.leafletMap) return;

  // Clear name filter
  STATE.searchQuery = '';
  document.getElementById('searchInput').value = '';

  // Clear location search
  document.getElementById('mapSearchInput').value = '';
  const resultsList = document.getElementById('mapSearchResults');
  resultsList.innerHTML = '';
  resultsList.classList.remove('open');

  // Update search center and load partners centered on this location
  STATE.searchCenter.lat = parseFloat(lat);
  STATE.searchCenter.lng = parseFloat(lng);

  // Hide "procurar aqui" button - we're doing the zoom
  document.getElementById('mapSearchBtn').style.display = 'none';

  // Set flag to indicate this is an auto-zoom, not a user pan
  STATE.isAutoZooming = true;

  // Zoom map to location
  STATE.leafletMap.setView([lat, lng], 13);

  // Load partners centered on this location
  loadMapPartners(lat, lng, false);
}

// ─── Map ──────────────────────────────────────────────────────────────────────
let mapResizeObserverActive = false;

function dbg(msg, keep = false) {
  const el = document.getElementById('mapDbg');
  el.style.display = 'block';
  el.textContent = msg;
  if (!keep) setTimeout(() => { el.style.display = 'none'; }, 5000);
}

function initMap() {
  const el = document.getElementById('map');
  const w = el.offsetWidth, h = el.offsetHeight;

  if (w < 10 || h < 10) {
    dbg('⚠ Container ' + w + '×' + h + ' – aguarda…', true);
    return false;
  }
  if (STATE.leafletMap) {
    STATE.leafletMap.invalidateSize();
    return true;
  }

  try {
    STATE.leafletMap = L.map('map', { zoomControl: true }).setView([STATE.searchCenter.lat, STATE.searchCenter.lng], 16);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap',
      maxZoom: 19
    }).addTo(STATE.leafletMap);

    STATE.leafletMap.on('tileerror', () => dbg('⚠ Erro ao carregar tiles.', true));

    STATE.leafletMap.on('movestart', () => {
      document.getElementById('mapMaxMsg').style.display = 'none';
    });

    STATE.leafletMap.on('moveend', () => {
      if (STATE.currentView !== 'map') return;
      const center = STATE.leafletMap.getCenter();
      STATE.searchCenter.lat = center.lat;
      STATE.searchCenter.lng = center.lng;

      // Show "procurar aqui" button only if user panned (not auto-zoom)
      if (!STATE.isAutoZooming) {
        document.getElementById('mapSearchBtn').style.display = 'block';
      }
      STATE.isAutoZooming = false; // Reset flag after move
    });

    STATE.mapInitialized = true;
    dbg('Mapa iniciado ' + w + '×' + h);
    return true;
  } catch (ex) {
    dbg('⚠ ' + ex.message, true);
    return false;
  }
}

function loadMapPartners(centerLat, centerLng, shouldFitBounds = false) {
  const params = new URLSearchParams({
    search: STATE.searchQuery,
    categories: Array.from(STATE.activeCats).join(','),
    lat: centerLat,
    lng: centerLng,
    limit: MAX_PINS,
  });

  fetch(`/api/map/search?${params}`)
    .then(r => r.json())
    .then(data => {
      clearMapMarkers();

      // Store location data for search/zoom
      STATE.mapLocationData = data.locations || [];

      // Reinstate user geolocated marker if available
      if (STATE.userLat != null && STATE.userLng != null) {
        const u = L.circleMarker([STATE.userLat, STATE.userLng], {
          radius: 9, fillColor: '#1a7f37', color: '#fff', weight: 2, fillOpacity: 0.9
        }).addTo(STATE.leafletMap).bindPopup('📍 A tua localização');
        STATE.markers.push(u);
      }

      const locations = data.locations || [];

      locations.forEach(loc => {
        if (loc.lat == null || loc.lng == null) return;

        const dStr = loc.distance != null ? `<br/><b>${formatDistance(loc.distance)}</b> de distância` : '';
        const popup = `<b>${escapeHtml(loc.partner_name)}</b><br/><small>${escapeHtml(loc.partner_category)}</small><br/>
          <em>${escapeHtml((loc.partner_discount || '').split(' – ')[0])}</em><br/>
          📍 ${escapeHtml(loc.location_name)}${dStr}
          ${loc.partner_website ? `<br/><a href="${escapeHtml(loc.partner_website)}" target="_blank">Website ↗</a>` : ''}`;

        STATE.markers.push(L.marker([loc.lat, loc.lng]).addTo(STATE.leafletMap).bindPopup(popup));
      });

      // Auto-zoom only on first load after view switch
      if (shouldFitBounds) {
        fitMapBounds();
      }

      dbg('✓ Max ' + (STATE.markers.length - (STATE.userLat != null ? 1 : 0)) + ' parceiros no mapa');
    })
    .catch(err => {
      dbg('⚠ Erro ao carregar parceiros: ' + err.message, true);
    });
}

function clearMapMarkers() {
  STATE.markers.forEach(m => m.remove());
  STATE.markers = [];
}

function renderMap() {
  if (!initMap()) {
    if (!mapResizeObserverActive) {
      mapResizeObserverActive = true;
      new ResizeObserver(entries => {
        for (const en of entries) {
          if (en.contentRect.width > 10 && en.contentRect.height > 10) {
            mapResizeObserverActive = false;
            renderMap();
            break;
          }
        }
      }).observe(document.getElementById('map'));
    }
    return;
  }

  document.getElementById('mapSearchBtn').style.display = 'none';
  document.getElementById('mapEmpty').style.display = 'none';

  // Load map with auto-zoom on first switch to map view
  const shouldZoom = STATE.firstMapLoadAfterSwitch;
  STATE.firstMapLoadAfterSwitch = false;
  loadMapPartners(STATE.searchCenter.lat, STATE.searchCenter.lng, shouldZoom);
}

// ─── View switch ──────────────────────────────────────────────────────────────
function setView(v) {
  STATE.currentView = v;
  const lv = document.getElementById('listView');
  const mv = document.getElementById('mapView');
  const lb = document.getElementById('listBtn');
  const mb = document.getElementById('mapBtn');

  sizeContent();

  if (v === 'list') {
    lv.style.display = '';
    mv.style.display = 'none';
    lb.classList.add('on');
    mb.classList.remove('on');
    renderList();
  } else {
    lv.style.display = 'none';
    mv.style.display = 'block';
    lb.classList.remove('on');
    mb.classList.add('on');
    STATE.firstMapLoadAfterSwitch = true; // Set flag to auto-zoom on switch
    requestAnimationFrame(() => requestAnimationFrame(renderMap));
  }
}

function render() {
  if (STATE.currentView === 'list') {
    renderList();
  } else {
    requestAnimationFrame(() => requestAnimationFrame(renderMap));
  }
}

// ─── Category pills ───────────────────────────────────────────────────────────
function buildPills() {
  const w = document.getElementById('cats');
  const all = document.createElement('span');
  all.className = 'pill on';
  all.textContent = 'Todos';
  all.onclick = () => {
    STATE.activeCats.clear();
    syncPills();
    render();
  };
  w.appendChild(all);

  STATE.cats.forEach(c => {
    const p = document.createElement('span');
    p.className = 'pill';
    p.textContent = c;
    p.dataset.c = c;
    p.onclick = () => {
      STATE.activeCats.has(c) ? STATE.activeCats.delete(c) : STATE.activeCats.add(c);
      syncPills();
      render();
    };
    w.appendChild(p);
  });
}

function syncPills() {
  document.querySelectorAll('.pill').forEach(p => {
    p.classList.toggle('on', p.dataset.c ? STATE.activeCats.has(p.dataset.c) : STATE.activeCats.size === 0);
  });
}

// ─── Geolocation ──────────────────────────────────────────────────────────────
function doGeo() {
  const btn = document.getElementById('nearBtn');
  const geoTxt = document.getElementById('geoTxt');

  if (!navigator.geolocation) {
    geoTxt.textContent = 'Geolocalização não suportada.';
    return;
  }

  btn.textContent = '⏳…';
  geoTxt.textContent = '';

  navigator.geolocation.getCurrentPosition(
    pos => {
      STATE.userLat = pos.coords.latitude;
      STATE.userLng = pos.coords.longitude;
      STATE.searchCenter.lat = pos.coords.latitude;
      STATE.searchCenter.lng = pos.coords.longitude;

      btn.textContent = '📍 +Proximo';
      btn.classList.add('on');
      document.getElementById('clearBtn').style.display = '';
      document.getElementById('sortSel').value = 'dist';
      geoTxt.textContent = `📍 ${STATE.userLat.toFixed(4)}, ${STATE.userLng.toFixed(4)}`;

      if (STATE.leafletMap && STATE.currentView === 'map') {
        STATE.leafletMap.setView([STATE.searchCenter.lat, STATE.searchCenter.lng], 16);
      }

      render();
    },
    err => {
      btn.textContent = '📍 +Prox.';
      geoTxt.textContent = '⚠ ' + err.message;
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

function clearGeo() {
  STATE.userLat = STATE.userLng = null;
  STATE.searchCenter.lat = STADIUM.lat;
  STATE.searchCenter.lng = STADIUM.lng;

  document.getElementById('nearBtn').classList.remove('on');
  document.getElementById('nearBtn').textContent = '📍 +Proximo';
  document.getElementById('clearBtn').style.display = 'none';
  document.getElementById('geoTxt').textContent = '';

  if (document.getElementById('sortSel').value === 'dist') {
    document.getElementById('sortSel').value = 'name';
  }

  if (STATE.leafletMap && STATE.currentView === 'map') {
    STATE.leafletMap.setView([STATE.searchCenter.lat, STATE.searchCenter.lng], 16);
  }

  render();
}

// ─── Layout sizing ────────────────────────────────────────────────────────────
function sizeContent() {
  const header = document.querySelector('header');
  const ctrl = document.getElementById('ctrl');
  const meta = document.getElementById('meta');
  const content = document.getElementById('content');

  // Use a small delay to ensure all elements are measured after layout
  requestAnimationFrame(() => {
    const used = (header?.offsetHeight || 52) + (ctrl?.offsetHeight || 80) + (meta?.offsetHeight || 20);
    const maxAvailable = window.innerHeight - used;
    const h = Math.max(100, maxAvailable);
    content.style.height = h + 'px';
  });
}

// ─── Event listeners ──────────────────────────────────────────────────────────
window.addEventListener('resize', () => {
  sizeContent();
  if (STATE.currentView === 'map') renderMap();
});

const aov = document.getElementById('aov');
document.getElementById('aboutBtn').onclick = () => aov.classList.add('open');
document.getElementById('acl').onclick = () => aov.classList.remove('open');
aov.onclick = ev => {
  if (ev.target === aov) aov.classList.remove('open');
};
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape') aov.classList.remove('open');
});

// ─── Boot ─────────────────────────────────────────────────────────────────────
async function boot() {
  try {
    const [pr, cr] = await Promise.all([
      fetch('/api/partners'),
      fetch('/api/categories')
    ]);

    const { partners } = await pr.json();
    const { categories } = await cr.json();

    STATE.all = partners;
    STATE.cats = categories;

    buildPills();

    // Mobile keyboard handler - prevent zoom when keyboard appears
    let lastHeight = window.innerHeight;
    window.addEventListener('resize', () => {
      const newHeight = window.innerHeight;
      if (newHeight < lastHeight * 0.7) {
        // Keyboard likely opened - reduce viewport to prevent content cutoff
        document.documentElement.style.zoom = (newHeight / lastHeight).toFixed(2);
      } else if (newHeight > lastHeight * 0.9) {
        // Keyboard closed - restore zoom
        document.documentElement.style.zoom = '1';
      }
      lastHeight = newHeight;
    });

    // Prevent zoom on input focus for mapSearchInput
    document.getElementById('mapSearchInput').addEventListener('focus', (e) => {
      e.target.style.fontSize = '16px'; // Prevent iOS zoom on focus
    });
    document.getElementById('mapSearchInput').addEventListener('blur', (e) => {
      e.target.style.fontSize = '';
    });

    document.getElementById('searchInput').addEventListener('input', ev => {
      STATE.searchQuery = ev.target.value.trim().toLowerCase();
      render();
    });

    document.getElementById('nearBtn').addEventListener('click', () => {
      if (STATE.userLat != null) {
        clearGeo();
      } else {
        doGeo();
      }
    });

    document.getElementById('clearBtn').addEventListener('click', clearGeo);
    document.getElementById('sortSel').addEventListener('change', render);
    document.getElementById('listBtn').addEventListener('click', () => setView('list'));
    document.getElementById('mapBtn').addEventListener('click', () => setView('map'));

    document.getElementById('mapSearchBtn').addEventListener('click', () => {
      document.getElementById('mapSearchBtn').style.display = 'none';
      loadMapPartners(STATE.searchCenter.lat, STATE.searchCenter.lng);
    });

    // Map search overlay events
    const mapSearchInput = document.getElementById('mapSearchInput');
    const mapSearchResults = document.getElementById('mapSearchResults');

    mapSearchInput.addEventListener('focus', () => {
      if (mapSearchResults.innerHTML) {
        mapSearchResults.classList.add('open');
      }
    });

    mapSearchInput.addEventListener('input', updateMapSearchResults);

    // Close search results on Escape key
    mapSearchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        mapSearchResults.classList.remove('open');
        mapSearchInput.blur();
      }
    });

    // Close results when clicking outside
    document.addEventListener('click', (e) => {
      if (!document.getElementById('mapSearchOverlay').contains(e.target)) {
        mapSearchResults.classList.remove('open');
      }
    });

    document.getElementById('loading').style.display = 'none';
    sizeContent();
    render();
  } catch (err) {
    document.getElementById('loading').innerHTML =
      `<div style="color:red;padding:20px">Erro ao carregar:<br>${err.message}</div>`;
  }
}

boot();
</script>
</body>
</html>"""


@app.route("/")
def index():
    html = (_HTML
            .replace("{{ about_claude_url }}", ABOUT_CLAUDE_URL)
            .replace("{{ about_email }}",      ABOUT_EMAIL)
            .replace("{{ about_kofi }}",       ABOUT_KOFI))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Benfica +Vantagens Explorer")
    parser.add_argument("--db",   default=DEFAULT_DB,   help=f"SQLite DB (default: {DEFAULT_DB})")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Use 0.0.0.0 to expose on LAN")
    args = parser.parse_args()

    global DB_PATH
    DB_PATH = args.db

    if not Path(DB_PATH).exists():
        logger.error(f"DB not found: {DB_PATH}")
        logger.info("Run the crawler first: python benfica_parceiros_crawler.py")
        return

    _validate_db_schema()

    logger.info(f"DB   : {DB_PATH}")
    logger.info(f"URL  : http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
