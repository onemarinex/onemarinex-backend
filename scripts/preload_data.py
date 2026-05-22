import sys
import os
from pathlib import Path
import json
from sqlalchemy import text
from datetime import datetime

# Add the project root to sys.path (scripts/ -> project root)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def preload_data():
    from app.db.session import SessionLocal
    
    file_path = PROJECT_ROOT / "preload_data.json"
    if not os.path.exists(file_path):
        print(f"Error: {file_path} not found.")
        return

    with open(file_path, 'r') as f:
        data = json.load(f)

    db = SessionLocal()
    
    def get_or_create_port_id(port_name):
        res = db.execute(text("SELECT id FROM ports WHERE name = :name"), {"name": port_name}).fetchone()
        if res:
            return res[0]
        
        print(f"Creating new port: {port_name}")
        code = "port_" + port_name.lower().replace(" ", "_").replace("&", "and").replace("/", "_")
        db.execute(text("INSERT INTO ports (name, code, is_active, created_at) VALUES (:name, :code, true, :now)"), 
                   {"name": port_name, "code": code, "now": datetime.utcnow()})
        db.commit()
        res = db.execute(text("SELECT id FROM ports WHERE name = :name"), {"name": port_name}).fetchone()
        return res[0]

    def clean_img_url(url):
        if not url or not isinstance(url, str): return None
        # If it doesn't look like a URL, it might be misaligned data
        if not url.startswith('http'): return None
        return url[:500] # Standard DB limit protection

    try:
        # 1. Hotels
        print("\n--- Processing Hotels ---")
        for item in data.get("Hotels", []):
            try:
                p_id = get_or_create_port_id(item['Port Name'])
                price = item.get('Price/Night (INR)') or item.get('Price/Night (USD)') or 0
                
                img_url = clean_img_url(item.get('Image URL'))
                desc = item.get('Description')
                if not img_url and len(str(item.get('Image URL'))) > 50:
                    desc = item.get('Image URL')
                
                existing = db.execute(text("SELECT id FROM hotels WHERE name = :name AND port_id = :port_id"), 
                                     {"name": item['Name'], "port_id": p_id}).fetchone()
                if existing:
                    print(f"Hotel {item['Name']} already exists, skipping.")
                    continue

                db.execute(text("""
                    INSERT INTO hotels (port_id, name, location, distance_from_port, rating, price_per_night, phone, image_url, description, address, lat, lng, created_at, updated_at)
                    VALUES (:port_id, :name, :location, :distance, :rating, :price, :phone, :img, :desc, :addr, :lat, :lng, :now, :now)
                """), {
                    "port_id": p_id, "name": item['Name'], "location": item['Location / Area'], 
                    "distance": float(item['Distance (km)']), "rating": float(item['Rating (★/5)']),
                    "price": float(price), "phone": str(item['Phone']) if item.get('Phone') else None,
                    "img": img_url, "desc": desc, "addr": item.get('Address'),
                    "lat": float(item['Latitude']), "lng": float(item['Longitude']), "now": datetime.utcnow()
                })
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"Skipping hotel {item.get('Name')}: {e}")

        # 2. Restaurants
        print("\n--- Processing Restaurants ---")
        for item in data.get("Restaurants", []):
            try:
                p_id = get_or_create_port_id(item['Port Name'])
                pop_for = item.get('Popular For', '').split(',') if isinstance(item.get('Popular For'), str) else []
                price = item.get('Price/Person (INR)') or item.get('Price/Person (USD)') or 0
                
                img_url = clean_img_url(item.get('Image URL'))
                desc = item.get('Description')
                if not img_url and len(str(item.get('Image URL'))) > 50:
                    desc = item.get('Image URL')

                existing = db.execute(text("SELECT id FROM restaurants WHERE name = :name AND port_id = :port_id"), 
                                     {"name": item['Name'], "port_id": p_id}).fetchone()
                if existing:
                    print(f"Restaurant {item['Name']} already exists, skipping.")
                    continue

                db.execute(text("""
                    INSERT INTO restaurants (port_id, name, location_name, distance_from_port, rating, price_per_person, timings, service_type, popular_for, phone, lat, lng, image_url, description, address, created_at, updated_at)
                    VALUES (:port_id, :name, :loc, :dist, :rate, :price, :time, :serv, :pop, :phone, :lat, :lng, :img, :desc, :addr, :now, :now)
                """), {
                    "port_id": p_id, "name": item['Name'], "loc": item['Location / Area'], "dist": float(item['Distance (km)']),
                    "rate": float(item['Rating (★/5)']), "price": float(price), "time": str(item.get('Timings')),
                    "serv": str(item.get('Service Type')), "pop": json.dumps([s.strip() for s in pop_for]),
                    "phone": str(item['Phone']) if item.get('Phone') else None, "lat": float(item['Latitude']),
                    "lng": float(item['Longitude']), "img": img_url, "desc": desc, "addr": item.get('Address'), "now": datetime.utcnow()
                })
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"Skipping restaurant {item.get('Name')}: {e}")

        # 3. Pubs
        print("\n--- Processing Pubs & Nightlife ---")
        pubs_data = data.get("Pubs & Nightlife") or data.get("Pubs") or []
        for item in pubs_data:
            try:
                p_id = get_or_create_port_id(item['Port Name'])
                pop_for = item.get('Popular For', '').split(',') if isinstance(item.get('Popular For'), str) else []
                price = item.get('Price/Person (INR)') or item.get('Price/Person (USD)') or 0
                
                img_url = clean_img_url(item.get('Image URL'))
                desc = item.get('Description')
                if not img_url and len(str(item.get('Image URL'))) > 50:
                    desc = item.get('Image URL')

                existing = db.execute(text("SELECT id FROM pubs WHERE name = :name AND port_id = :port_id"), 
                                     {"name": item['Name'], "port_id": p_id}).fetchone()
                if existing:
                    print(f"Pub {item['Name']} already exists, skipping.")
                    continue

                db.execute(text("""
                    INSERT INTO pubs (port_id, name, location_name, distance_from_port, rating, price_per_person, timings, service_type, popular_for, phone, lat, lng, image_url, description, created_at, updated_at)
                    VALUES (:port_id, :name, :loc, :dist, :rate, :price, :time, :serv, :pop, :phone, :lat, :lng, :img, :desc, :now, :now)
                """), {
                    "port_id": p_id, "name": item['Name'], "loc": item['Location / Area'], "dist": float(item['Distance (km)']),
                    "rate": float(item['Rating (★/5)']), "price": float(price), "time": str(item.get('Timings')),
                    "serv": str(item.get('Service Type')), "pop": json.dumps([s.strip() for s in pop_for]),
                    "phone": str(item['Phone']) if item.get('Phone') else None, "lat": float(item['Latitude']),
                    "lng": float(item['Longitude']), "img": img_url, "desc": desc, "now": datetime.utcnow()
                })
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"Skipping pub {item.get('Name')}: {e}")

        # 4. Sightseeing
        print("\n--- Processing Sightseeing ---")
        for item in data.get("Sightseeing", []):
            try:
                p_id = get_or_create_port_id(item['Port Name'])
                price = item.get('Price/Person (INR)') or item.get('Price/Person (USD)') or 0
                
                img_url = clean_img_url(item.get('Image URL'))
                desc = item.get('Description')
                if not img_url and len(str(item.get('Image URL'))) > 50:
                    desc = item.get('Image URL')

                existing = db.execute(text("SELECT id FROM sightseeings WHERE name = :name AND port_id = :port_id"), 
                                     {"name": item['Name'], "port_id": p_id}).fetchone()
                if existing:
                    print(f"Sightseeing {item['Name']} already exists, skipping.")
                    continue

                db.execute(text("""
                    INSERT INTO sightseeings (port_id, name, location_name, distance_from_port, rating, price_per_person, phone, lat, lng, image_url, description, address, created_at, updated_at)
                    VALUES (:port_id, :name, :loc, :dist, :rate, :price, :phone, :lat, :lng, :img, :desc, :addr, :now, :now)
                """), {
                    "port_id": p_id, "name": item['Name'], "loc": item['Location / Area'], "dist": float(item['Distance (km)']),
                    "rate": float(item['Rating (★/5)']), "price": float(price), "phone": str(item['Phone']) if item.get('Phone') else None,
                    "lat": float(item['Latitude']), "lng": float(item['Longitude']), "img": img_url, "desc": desc, "addr": item.get('Address'), "now": datetime.utcnow()
                })
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"Skipping sightseeing {item.get('Name')}: {e}")
        print(f"Successfully loaded Sightseeing.")

    except Exception as e:
        db.rollback()
        print(f"Error during preloading: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    preload_data()
