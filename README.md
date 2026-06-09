# StudyMate Elite (v4.0) - Telegram Productivity Bot

StudyMate is a premium, timezone-aware Telegram companion built to maximize student focus, track targets, and organize note vault libraries.

## 🚀 Commands

- `/start` - Launch the Nexus UI and register.
- `/reminder [task] [time]` - Set a new study target (e.g. `/reminder study math in 20m` or `/reminder review biology tomorrow at 9am`).
- `/list` - View and delete pending targets.
- `/pomodoro [minutes]` - Activate deep focus mode (defaults to 25m).
- `/timezone [offset]` - Synchronize the bot with your local time (e.g. `/timezone +5.5` for IST).
- `/time` - Inspect current synchronized bot time.
- `/stats` - Monitor accumulated focus hours and XP.
- `/note [content]` - Store notes, documents, or photos in the secure vault library.
- `/notes` - Browse stored library.
- `/quiz` - Take an AI-driven conceptual test.
- `/report` - Access weekly success analytics.
- `/clear` - Clean up completed scheduler targets.

## ⚙️ Setup & Configuration

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a `.env` file in the root folder with:
   ```env
   BOT_TOKEN=your_telegram_bot_token_here
   ```
3. Run the watchdog runner locally:
   - On Windows: Run `deploy_nexus.vbs` or `start_nexus.bat`
   - Otherwise: `python bot.py`

## 🌍 Cloud Deployment (Railway)

1. Make sure a `Procfile` is present with `worker: python bot.py`.
2. Push repository code to GitHub.
3. Link the repository to Railway.
4. Add the `BOT_TOKEN` in the Railway environment variables.
