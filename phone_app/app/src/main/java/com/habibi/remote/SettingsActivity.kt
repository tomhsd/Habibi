package com.habibi.remote

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebView
import androidx.core.app.NotificationManagerCompat
import org.json.JSONArray
import org.json.JSONObject

/** Animated HUD screen (assets/settings.html) listing every permission Habibi uses. */
class SettingsActivity : Activity() {

    private lateinit var web: WebView

    private val corePerms = arrayOf(
        Manifest.permission.SEND_SMS, Manifest.permission.CALL_PHONE,
        Manifest.permission.READ_CONTACTS, Manifest.permission.ACCESS_FINE_LOCATION,
        Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.RECORD_AUDIO
    )

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        web.setBackgroundColor(Pal.BG)
        web.settings.javaScriptEnabled = true
        web.webChromeClient = WebChromeClient()          // shows the "Unpair?" confirm dialog
        web.addJavascriptInterface(Bridge(), "HabibiSettings")
        setContentView(web)
        web.loadUrl("file:///android_asset/settings.html")
    }

    override fun onResume() {
        super.onResume()
        refresh()          // coming back from an Android settings screen
    }

    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        refresh()
    }

    private fun refresh() {
        if (::web.isInitialized) web.evaluateJavascript("window.refresh && window.refresh()", null)
    }

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun item(key: String, title: String, desc: String, ok: Boolean) =
        JSONObject().put("key", key).put("title", title).put("desc", desc).put("ok", ok)

    inner class Bridge {
        @JavascriptInterface
        fun status(): String {
            val c = this@SettingsActivity
            val items = JSONArray()
            if (Build.VERSION.SDK_INT >= 29) {
                items.put(item("overlay", "Display over other apps",
                    "Open apps, links and calls when you ask.", Settings.canDrawOverlays(c)))
            }
            items.put(item("a11y", "Accessibility → Habibi phone control",
                "Home / Back / Recents, lock, tap things, type text, read the screen.",
                HabibiA11y.instance != null))
            items.put(item("notif", "Notification access",
                "Read your recent notifications to you.",
                NotificationManagerCompat.getEnabledListenerPackages(c).contains(packageName)))
            items.put(item("perms", "Texts, calls, contacts, location, mic",
                "Texts and calls you confirm on the PC, contacts by name, where is my phone, live voice.",
                corePerms.all { granted(it) }))
            if (Build.VERSION.SDK_INT >= 29) {
                items.put(item("bgloc", "Location: allow all the time",
                    "Find the phone while the app is closed. Choose \"Allow all the time\".",
                    granted(Manifest.permission.ACCESS_BACKGROUND_LOCATION)))
            }
            val pm = getSystemService(PowerManager::class.java)
            items.put(item("battery", "Keep the link running",
                "Stops Android from closing the connection to Habibi.",
                pm.isIgnoringBatteryOptimizations(packageName)))
            return JSONObject()
                .put("items", items)
                .put("server", Prefs.server(c))
                .put("linked", HabibiService.connected)
                .toString()
        }

        @SuppressLint("BatteryLife")
        @JavascriptInterface
        fun allow(key: String) {
            runOnUiThread {
                val pkg = Uri.parse("package:$packageName")
                try {
                    when (key) {
                        "overlay" -> startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, pkg))
                        "a11y" -> startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                        "notif" -> startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
                        "perms" -> requestPermissions(corePerms, 3)
                        "bgloc" ->
                            if (!granted(Manifest.permission.ACCESS_FINE_LOCATION)) requestPermissions(corePerms, 3)
                            else requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), 4)
                        "battery" -> startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, pkg))
                    }
                } catch (e: Exception) {
                    startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, pkg))
                }
            }
        }

        @JavascriptInterface
        fun close() { runOnUiThread { finish() } }

        @JavascriptInterface
        fun unpair() {
            runOnUiThread {
                HabibiService.stop(this@SettingsActivity)
                Prefs.clear(this@SettingsActivity)
                finish()
            }
        }
    }
}
