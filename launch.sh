#!/bin/bash
set -e

echo "🚀 Preparing local environment..."

# 1. Start the database
echo "📦 Starting local Postgres database via Docker..."
if command -v docker-compose &> /dev/null; then
    docker-compose up -d
elif docker compose version &> /dev/null; then
    docker compose up -d
else
    echo "❌ Error: Docker Compose is not installed. Please install Docker Compose."
    exit 1
fi

echo "⏳ Waiting for database to be ready..."
sleep 5

# 2. Check virtual environment
if [ ! -d ".venv" ] && [ ! -d "venv" ]; then
    echo "🛠️  Creating virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
fi

# 3. Ensure pip is installed and update requirements
echo "📦 Installing dependencies..."
pip install -r requirements.txt

# 4. Install playwright browsers
echo "🌐 Installing Playwright Chromium browser..."
python -m playwright install chromium

# 5. Initialize the database schema
echo "🗄️  Initializing database schema..."
python scripts/init_supabase.py

# 6. Check for bot token
if grep -q "TELEGRAM_BOT_TOKEN=$" .env; then
    echo "⚠️  WARNING: TELEGRAM_BOT_TOKEN is empty in .env file."
    echo "Please add your Telegram bot token to the .env file before running the bot."
    echo "You can get one from @BotFather on Telegram."
    exit 1
fi

echo "✅ Setup complete! Starting the bot..."
python telegram_bot.py
