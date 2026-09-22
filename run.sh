#!/bin/bash
# =========================================================
# Run FastAPI (Uvicorn) safely on port 8000
# - Automatically kills any old process on that port
# - Frees the port on exit (Ctrl + C)
# =========================================================

PORT=8000
APP="app.server:app"  # Path to FastAPI app

echo "🔍 Checking if port $PORT is already in use..."
PID=$(lsof -t -i:$PORT)

if [ -n "$PID" ]; then
    echo "⚠️  Port $PORT is in use by PID $PID. Killing it..."
    kill -9 $PID
    echo "✅ Freed port $PORT."
else
    echo "✅ Port $PORT is free."
fi

# Clean up on exit
trap "echo '🧹 Cleaning up...'; fuser -k ${PORT}/tcp >/dev/null 2>&1" EXIT

echo "🚀 Starting FastAPI server on port $PORT..."
uvicorn $APP --reload --host 0.0.0.0 --port $PORT


# git add Dockerfile
# git commit -m "add Dockerfile"
# git push
# ```

# ---

# **Step 4 — Set your region**
# - Pick **Singapore (`sin`)** — closest to India

# ---

# **Step 5 — Add your `.env` secrets**
# - In the web UI, look for **Secrets** or **Environment Variables** section
# - Add each key-value pair from your `.env` file there

# ---

# **Step 6 — Click Deploy 🚀**

# ---

# **After deploy, your server will be live at:**
# ```
# https://your-app-name.fly.dev