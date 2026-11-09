import mysql.connector
import pymongo
import time
import random

print("=== Connection Pooling and Retry Logic Test ===")
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
cursor_primary = sql_primary.cursor()
cursor_replica = sql_replica.cursor()

mongo_client = pymongo.MongoClient("mongodb://mongodb:27017/")
mongo_db = mongo_client["chat_db"]
mongo_messages = mongo_db["messages"]

print("\n--- EXPERIMENT 1: Ingesting 10,000 Messages (SQL vs NoSQL) ---")
messages_sample = [
    (random.randint(1, 500), 
     random.randint(100, 999), 
     f"Message {i}") for i in range(10000)
]

print("Inserting messages into MySQL primary...")
start = time.time()
cursor_primary.executemany("INSERT INTO messages_indexed (conversation_id, user_id, message_body) VALUES (%s, %s, %s)", messages_sample)
sql_primary.commit()
end = time.time()
print(f"MySQL Insertion Time: {end - start:.2f} seconds")

print("=== NoSQL Batch write ====")
mongo_docs = [{
    "conversation_id": c,
    "user_id": u,
    "message_body": m,
    "created_at": time.time()
    } for c, u, m in messages_sample]

start = time.time()
mongo_messages.insert_many(mongo_docs)
mongo_time = (time.time() - start) * 1000
print(f"✓ NoSQL (MongoDB Document Store) 10,000 Inserts: {mongo_time} ms")

print("==== Populate unindexed sql table for comparison")
cursor_primary.executemany("INSERT INTO messages_unindexed (conversation_id, user_id, message_body) VALUES (%s, %s, %s)", messages_sample)
sql_primary.commit()


print("\n--- EXPERIMENT 2: B-Tree Composite vs Full Table Scan ---")
target_conv = 42

start_time = time.time()
cursor_replica.execute("SELECT * FROM messages_indexed WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 20", (target_conv,))
cursor_replica.fetchall()
indexed_time = (time.time() - start_time) * 1000
print(f"✓ SQL (B-Tree Composite Index) Query Time: {indexed_time:.2f} ms")

start_time = time.time()
cursor_replica.execute("SELECT * FROM messages_unindexed WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 20", (target_conv,))
cursor_replica.fetchall()
scan_time = (time.time() - start_time) * 1000
print(f"✓ SQL (Full Table Scan) Query Time: {scan_time:.2f} ms")

print("\n--- EXPERIMENT 3: Hash-Based Application Sharding Router ---")
num_shards = 2

def get_shard(conversation_id):
    shard_id = hash(conversation_id) % num_shards
    return shard_id

sample_conversations = [random.randint(1, 100) for _ in range(10)]

for conversation_id in sample_conversations:
    target_shard = get_shard(conversation_id)
    print(f"Conversation ID {conversation_id} routed to Shard {target_shard}")
