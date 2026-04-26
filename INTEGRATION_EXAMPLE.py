"""
Complete Integration Example for Update System

Shows how to integrate update notifications into app.py

Copy-paste the relevant sections into your app.py file.
"""

# ═══════════════════════════════════════════════════════════════════════════
# 1. IMPORTS (Add at top of app.py)
# ═══════════════════════════════════════════════════════════════════════════

from flask import Flask, render_template, jsonify
from app_update_routes import register_update_routes
# ... other imports ...


# ═══════════════════════════════════════════════════════════════════════════
# 2. CREATE FLASK APP (After creating app instance)
# ═══════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# ... other app configuration ...

# Register update notification routes
register_update_routes(app)

# ═══════════════════════════════════════════════════════════════════════════
# 3. Update context processor (optional - pass update status to templates)
# ═══════════════════════════════════════════════════════════════════════════

@app.context_processor
def inject_update_status():
    """Make update notifier available in templates."""
    from update_notifier import get_notifier
    try:
        notifier = get_notifier()
        return {
            'update_available': notifier.update_available,
            'update_version': notifier.update_version,
        }
    except Exception:
        return {
            'update_available': False,
            'update_version': None,
        }

# ═══════════════════════════════════════════════════════════════════════════
# 4. ADD TO templates/base.html
# ═══════════════════════════════════════════════════════════════════════════

# Add this in base.html <body> (near top):
"""
<!-- Update Notification Container -->
<div id="update-notification-container"></div>

<!-- Other content ... -->

<!-- Add before </body> -->
<script src="{{ url_for('static', filename='js/update_notifier.js') }}"></script>
"""

# ═══════════════════════════════════════════════════════════════════════════
# 5. OPTIONAL: Add menu item for manual check
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/check-updates')
def check_updates_page():
    """Page to manually check for updates."""
    return render_template('check_updates.html')


# ═══════════════════════════════════════════════════════════════════════════
# EXAMPLE: templates/check_updates.html
# ═══════════════════════════════════════════════════════════════════════════

CHECK_UPDATES_TEMPLATE = """
{% extends "base.html" %}

{% block title %}Check for Updates{% endblock %}

{% block content %}
<div class="container" style="max-width: 600px; margin-top: 40px;">
    <h2>Check for Updates</h2>

    <div id="update-container" style="margin: 20px 0;">
        <button id="check-btn" onclick="checkUpdates()" style="
            background-color: #007bff;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
        ">Check for Updates</button>
    </div>

    <div id="status-container" style="margin: 20px 0; min-height: 50px;"></div>
</div>

<script>
async function checkUpdates() {
    const btn = document.getElementById('check-btn');
    const container = document.getElementById('status-container');

    btn.disabled = true;
    btn.textContent = 'Checking...';
    container.innerHTML = '<p>Checking for updates...</p>';

    try {
        const response = await fetch('/api/updates/check', { method: 'POST' });
        const data = await response.json();

        if (data.update_available) {
            container.innerHTML = `
                <div style="
                    background-color: #d1ecf1;
                    border: 1px solid #b8daff;
                    padding: 15px;
                    border-radius: 4px;
                ">
                    <strong>Update Available!</strong>
                    <p>Version ${data.version} is available.</p>
                    <button onclick="installUpdate()" style="
                        background-color: #17a2b8;
                        color: white;
                        padding: 8px 16px;
                        border: none;
                        border-radius: 4px;
                        cursor: pointer;
                    ">Install Update</button>
                </div>
            `;
        } else {
            container.innerHTML = `
                <div style="
                    background-color: #d1e7dd;
                    border: 1px solid #badbcc;
                    padding: 15px;
                    border-radius: 4px;
                ">
                    <strong>✓ Already Up to Date</strong>
                    <p>You are running the latest version.</p>
                </div>
            `;
        }
    } catch (error) {
        container.innerHTML = `
            <div style="
                background-color: #f8d7da;
                border: 1px solid #f5c6cb;
                padding: 15px;
                border-radius: 4px;
                color: #721c24;
            ">
                <strong>Error</strong>
                <p>${error.message}</p>
            </div>
        `;
    } finally {
        btn.disabled = false;
        btn.textContent = 'Check for Updates';
    }
}

async function installUpdate() {
    if (!confirm('Download and install update? Application will restart.')) {
        return;
    }

    try {
        const response = await fetch('/api/updates/install', { method: 'POST' });
        const data = await response.json();

        if (data.status === 'installing') {
            alert('Update installation started. Application will restart shortly.');
        } else {
            alert('Failed to start installation');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}
</script>
{% endblock %}
"""

# ═══════════════════════════════════════════════════════════════════════════
# That's it! Summary of changes:
# ═══════════════════════════════════════════════════════════════════════════

"""
CHECKLIST:

1. ✅ Add imports to app.py:
   - from app_update_routes import register_update_routes

2. ✅ Register routes in app.py:
   - register_update_routes(app)

3. ✅ Add HTML container to templates/base.html:
   - <div id="update-notification-container"></div>

4. ✅ Add JavaScript include to templates/base.html:
   - <script src="{{ url_for('static', filename='js/update_notifier.js') }}"></script>

5. ✅ (Optional) Add context processor for templates:
   - @app.context_processor def inject_update_status():...

6. ✅ (Optional) Create check_updates.html for manual checking

RESULT:
- ✓ Auto-install on startup (launcher.py)
- ✓ In-app notification banner
- ✓ Background checks every hour
- ✓ Manual check button
- ✓ One-click install
- ✓ Automatic restart after update

That's all! 🚀
"""
