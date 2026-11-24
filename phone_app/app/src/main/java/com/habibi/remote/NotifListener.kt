package com.habibi.remote

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

/** Keeps the 30 most recent notifications so Habibi can read them out. */
class NotifListener : NotificationListenerService() {

    companion object {
        val recent = CopyOnWriteArrayList<JSONObject>()
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        try { activeNotifications?.forEach { add(it) } } catch (_: Exception) {}
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) { add(sbn) }

    private fun add(sbn: StatusBarNotification) {
        if (sbn.packageName == packageName) return
        val ex = sbn.notification.extras
        val title = ex.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = ex.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
        if (title.isEmpty() && text.isEmpty()) return
        val app = try {
            packageManager.getApplicationLabel(packageManager.getApplicationInfo(sbn.packageName, 0)).toString()
        } catch (e: Exception) { sbn.packageName }
        recent.removeIf { it.optString("app") == app && it.optString("title") == title && it.optString("text") == text }
        recent.add(0, JSONObject().put("app", app).put("title", title).put("text", text).put("time", sbn.postTime))
        while (recent.size > 30) recent.removeAt(recent.size - 1)
    }
}
