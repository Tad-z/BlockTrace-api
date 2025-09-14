import os
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from routes.user import router as user_router
from routes.wallet import router as wallet_router
from routes.data import router as data_router
from routes.stripe import router as stripe_router
from routes.exports import router as exports_router

# Load environment variables
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

if not MONGO_URI or not DB_NAME:
    raise ValueError("MONGO_URI or DB_NAME is missing in environment variables.")

# FastAPI application
app = FastAPI()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    # ✅ Initialize MongoDB connection first
    app.state.client = AsyncIOMotorClient(MONGO_URI)
    app.state.db = app.state.client[DB_NAME]
    print("MongoDB connection established successfully!")

@app.on_event("shutdown")
async def shutdown_event():
    if app.state.client:
        app.state.client.close()
        print("MongoDB connection closed!")

@app.get("/health")
async def root():
    return {"message": "BlockTrace"}

# Include routers
app.include_router(user_router, tags=["User"], prefix="/user")
app.include_router(wallet_router, tags=["Wallet"], prefix="/wallet")
app.include_router(data_router, tags=["Data"], prefix="/data")
app.include_router(stripe_router, tags=["Stripe"], prefix="/stripe")
app.include_router(exports_router, tags=["Exports"], prefix="/export")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)