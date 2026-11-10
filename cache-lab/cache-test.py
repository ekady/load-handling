import mysql.connector
import redis
import time
import random

print("=== Data layer and caching performance benchmark ===")

sql_primary = mysql.connector.connect(
    host="mysql-primary",
    user="root",
    password="my-secret-pw",
    database="chat_db"
)
sql_replica = mysql.connector.connect(
    host="mysql-replica",
    user="root",
    password="my-secret-pw",
    database="chat_db"
)
cursor_p = sql_primary.cursor()
cursor_r = sql_replica.cursor()

cache = redis.Redis(host='redis-cache', port=6379, db=0, decode_responses=True)

print("\n--- EXPERIMENT 1: Ingesting 500,000 Messages (SQL vs Cache) ---")
cursor_r.execute("SELECT COUNT(*) FROM messages_indexed")
current_count = cursor_r.fetchone()[0]

if current_count < 500000:
    needed = 500000 - current_count
    print(f"Populating MySQL with {needed} messages...")
    batch_size = 500000
    batches = (needed // batch_size) + 1
    for b in range(batches):
        messages_sample = [
            (random.randint(1, 1000), 
             random.randint(100, 999), 
             f"Message {i}") for i in range(batch_size)
        ]
        cursor_p.executemany("INSERT INTO messages_indexed (conversation_id, user_id, message_body) VALUES (%s, %s, %s)", messages_sample)
        cursor_p.executemany("INSERT INTO messages_unindexed (conversation_id, user_id, message_body) VALUES (%s, %s, %s)", messages_sample)
        sql_primary.commit()
        print(f"Inserted batch {b+1}/{batches} of {batch_size} messages into MySQL.")
    print("MySQL population complete.")
else:
    print("MySQL already has 500,000 messages. Skipping population.")

time.sleep(1)  # Ensure data is committed before timing

print("\n--- EXPERIMENT 2: Benchmark 4 performance tiers ---")

# Tier 1: Unindexed SQL Table Scan
target_conv = 42
t1_start = time.time()
cursor_r.execute("SELECT * FROM messages_unindexed WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 20", (target_conv,))
cursor_r.fetchall()
t1_end = time.time()
print(f"Tier 1 took {t1_end - t1_start:.4f} seconds.")

# Tier 2: Non covering B-Tree Index Query
t2_start = time.time()
cursor_r.execute("SELECT * FROM messages_indexed WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 20", (target_conv,))
cursor_r.fetchall()
t2_end = time.time()
print(f"Tier 2 took {t2_end - t2_start:.4f} seconds.")

# Tier 3: Covering Index Query
t3_start = time.time()
cursor_r.execute("SELECT user_id, message_body FROM messages_indexed WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 20", (target_conv,))
t3_result = cursor_r.fetchall()
t3_end = time.time()
print(f"Tier 3 took {t3_end - t3_start:.4f} seconds.")

# Tier 4: Redis Cache Lookup
cache_key = f"messages:{target_conv}"
cache.set(cache_key, t3_result.__str__(), ex=60)  # Ensure the cache key is empty for testing

t4_start = time.time()
cached_result = cache.get(cache_key)
if not cached_result:
    cursor_r.execute("SELECT * FROM messages_indexed WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 20", (target_conv,))
    cached_result = cursor_r.fetchall()
    cache.set(cache_key, cached_result.__str__(), ex=60)  # Cache for 60 seconds
t4_end = time.time()
print(f"Tier 4 took {t4_end - t4_start:.4f} seconds.")

print("\n-- Summary of performance tiers --")
print(f"Tier 1 (Unindexed SQL): {t1_end - t1_start:.4f} seconds")
print(f"Tier 2 (Non-covering B-Tree): {t2_end - t2_start:.4f} seconds")
print(f"Tier 3 (Covering Index): {t3_end - t3_start:.4f} seconds")
print(f"Tier 4 (Redis Cache): {t4_end - t4_start:.4f} seconds")
