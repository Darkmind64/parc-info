/**
 * ParcInfo Update Notifier
 *
 * Displays update notifications in the web interface
 * - Shows banner when update available
 * - Handles install and dismiss actions
 * - Auto-checks for updates periodically
 */

class UpdateNotifier {
    constructor(options = {}) {
        this.checkInterval = options.checkInterval || 3600000; // 1 hour
        this.containerId = options.containerId || 'update-notification-container';
        this.autoCheck = options.autoCheck !== false;
        this.notificationTemplate = options.notificationTemplate || this.defaultTemplate;

        this.currentNotification = null;
        this.checkTimeout = null;

        if (this.autoCheck) {
            this.startAutoCheck();
        }
    }

    /**
     * Start periodic update checks
     */
    startAutoCheck() {
        // Check immediately
        this.checkForUpdates();

        // Then check periodically
        this.checkTimeout = setInterval(() => {
            this.checkForUpdates();
        }, this.checkInterval);
    }

    /**
     * Stop periodic checks
     */
    stopAutoCheck() {
        if (this.checkTimeout) {
            clearInterval(this.checkTimeout);
            this.checkTimeout = null;
        }
    }

    /**
     * Check for updates
     */
    async checkForUpdates() {
        try {
            const response = await fetch('/api/updates/status');
            const data = await response.json();

            if (data.notification) {
                this.showNotification(data.notification);
            } else {
                this.hideNotification();
            }
        } catch (error) {
            console.error('Failed to check for updates:', error);
        }
    }

    /**
     * Show update notification
     */
    showNotification(notification) {
        this.currentNotification = notification;
        const container = document.getElementById(this.containerId);

        if (!container) {
            console.warn(`Container #${this.containerId} not found`);
            return;
        }

        const html = this.notificationTemplate(notification);
        container.innerHTML = html;

        // Setup event handlers
        const installBtn = container.querySelector('[data-action="install"]');
        const dismissBtn = container.querySelector('[data-action="dismiss"]');

        if (installBtn) {
            installBtn.addEventListener('click', () => this.installUpdate());
        }
        if (dismissBtn) {
            dismissBtn.addEventListener('click', () => this.dismissNotification());
        }
    }

    /**
     * Hide notification
     */
    hideNotification() {
        this.currentNotification = null;
        const container = document.getElementById(this.containerId);
        if (container) {
            container.innerHTML = '';
        }
    }

    /**
     * Install update
     */
    async installUpdate() {
        const installBtn = document.querySelector('[data-action="install"]');
        if (installBtn) {
            installBtn.disabled = true;
            installBtn.textContent = 'Installing...';
        }

        try {
            const response = await fetch('/api/updates/install', { method: 'POST' });
            const data = await response.json();

            if (data.status === 'installing') {
                // Update UI to show installation in progress
                const notification = data.notification;
                if (notification) {
                    this.showNotification(notification);
                }

                // Show success message
                alert('Update installation started. Application will restart shortly.');

                // Start monitoring for completion
                this.monitorInstallation();
            } else {
                alert('Failed to start update installation');
                if (installBtn) {
                    installBtn.disabled = false;
                    installBtn.textContent = 'Install Update';
                }
            }
        } catch (error) {
            console.error('Failed to install update:', error);
            alert('Error installing update: ' + error.message);
            if (installBtn) {
                installBtn.disabled = false;
                installBtn.textContent = 'Install Update';
            }
        }
    }

    /**
     * Dismiss notification
     */
    async dismissNotification() {
        try {
            await fetch('/api/updates/dismiss', { method: 'POST' });
            this.hideNotification();
        } catch (error) {
            console.error('Failed to dismiss notification:', error);
        }
    }

    /**
     * Monitor installation progress
     */
    monitorInstallation() {
        let checkCount = 0;
        const maxChecks = 60; // 60 checks * 5 sec = 5 minutes

        const monitor = setInterval(async () => {
            checkCount++;

            try {
                const response = await fetch('/api/updates/status');
                const data = await response.json();

                if (data.notification && data.notification.type === 'update_complete') {
                    clearInterval(monitor);
                    alert('Update installed! Application will restart.');
                    // App should restart soon
                } else if (checkCount >= maxChecks) {
                    clearInterval(monitor);
                }
            } catch (error) {
                console.error('Monitoring error:', error);
                if (checkCount >= maxChecks) {
                    clearInterval(monitor);
                }
            }
        }, 5000); // Check every 5 seconds
    }

    /**
     * Default notification template
     */
    defaultTemplate(notification) {
        let icon = '⚠️';
        let bgColor = '#fff3cd';
        let borderColor = '#ffc107';
        let buttonText = 'Install Update';

        if (notification.type === 'update_available') {
            icon = '📦';
            bgColor = '#d1ecf1';
            borderColor = '#17a2b8';
        } else if (notification.type === 'installing') {
            icon = '⏳';
            bgColor = '#d1e7dd';
            borderColor = '#198754';
            buttonText = 'Installing...';
        } else if (notification.type === 'update_complete') {
            icon = '✅';
            bgColor = '#d1e7dd';
            borderColor = '#198754';
            buttonText = 'Restarting...';
        }

        return `
            <div style="
                background-color: ${bgColor};
                border-left: 4px solid ${borderColor};
                padding: 16px;
                margin-bottom: 16px;
                border-radius: 4px;
                display: flex;
                justify-content: space-between;
                align-items: center;
            ">
                <div style="flex: 1;">
                    <div style="font-weight: bold; margin-bottom: 4px;">
                        ${icon} ${notification.message}
                    </div>
                    ${notification.version ? `
                        <div style="font-size: 0.9em; color: #666;">
                            Version ${notification.version}
                        </div>
                    ` : ''}
                </div>
                <div style="display: flex; gap: 8px; margin-left: 16px;">
                    ${notification.type === 'update_available' ? `
                        <button
                            data-action="install"
                            style="
                                background-color: #17a2b8;
                                color: white;
                                border: none;
                                padding: 8px 16px;
                                border-radius: 4px;
                                cursor: pointer;
                                font-weight: bold;
                            "
                        >${buttonText}</button>
                    ` : ''}
                    <button
                        data-action="dismiss"
                        style="
                            background-color: transparent;
                            color: #666;
                            border: 1px solid #ddd;
                            padding: 8px 16px;
                            border-radius: 4px;
                            cursor: pointer;
                        "
                    >Dismiss</button>
                </div>
            </div>
        `;
    }
}

// Auto-initialize if in browser
if (typeof document !== 'undefined' && document.readyState !== 'loading') {
    window.updateNotifier = new UpdateNotifier();
}
