
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from dotenv import load_dotenv
import os,redis,razorpay

load_dotenv()

def connect_to_mongodb():
    uri = os.getenv("MONGODB_URL")

    client = MongoClient(uri, server_api=ServerApi('1'))

    try:
        client.admin.command('ping')
        DATABASE_NAME = os.getenv("DATABASE_NAME")
        db = client[DATABASE_NAME]
        print(db.list_collection_names())
        print("Pinged your deployment. You successfully connected to MongoDB!")
        
        return db
    except Exception as e:
        print(e)


def connect_to_redis():
    try:
        client = redis.Redis(
            host=os.getenv("REDIS_HOST"),
            port=int(os.getenv("REDIS_PORT")),
            username=os.getenv("REDIS_USERNAME"),
            password=os.getenv("REDIS_PASSWORD"),
            decode_responses=True
        )

        client.ping()

        print("Redis connected successfully!")

        return client

    except Exception as e:
        print(e)
        return None


def connect_to_razorpay():
    try:
        client = razorpay.Client(
            auth=(
                os.getenv("RAZORPAY_KEY_ID"),
                os.getenv("RAZORPAY_KEY_SECRET")
            )
        )

        client.plan.all()

        print("Razorpay connected successfully!")

        return client

    except Exception as e:
        print(e)
        return None

