from fastapi import FastAPI, Depends
from pymongo import MongoClient
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import os

load_dotenv()


# db.py


def get_db(app: FastAPI):
    if not hasattr(app.state, "db"):
        raise RuntimeError("Database connection is not initialized.")
    return app.state.db



