package com.habibi.remote

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Re-opens the link to Habibi after the phone restarts. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED && Prefs.isPaired(context)) {
            try { HabibiService.start(context) } catch (_: Exception) {}
        }
    }
}
