package com.habibi.remote

import android.Manifest
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.location.Location
import android.location.LocationManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.RingtoneManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.provider.ContactsContract
import android.provider.MediaStore
import android.provider.Settings
import android.telephony.SmsManager
import android.view.KeyEvent
import androidx.core.app.NotificationManagerCompat
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Runs one Habibi command on the phone. Every action returns a [Res]; nothing throws. */
class CommandExecutor(private val ctx: Context) {

    data class Res(val ok: Boolean, val result: Any? = null, val error: String? = null)

    private val main = Handler(Looper.getMainLooper())
    private val am: AudioManager get() = ctx.getSystemService(AudioManager::class.java)

    fun run(action: String, p: JSONObject): Res = try {
        when (action) {
            "open_app" -> openApp(p.optString("app"))
            "list_apps" -> Res(true, JSONArray(launcherApps().map { it.first }.sorted()))
            "open_url" -> openUrl(p.optString("url"))
            "media" -> media(p.optString("command", "play_pause"))
            "volume" -> volume(p)
            "flashlight" -> flashlight(p.optBoolean("on", true))
            "find_phone" -> findPhone()
            "battery" -> Res(true, battery())
            "notifications" -> notifications()
            "send_sms" -> sendSms(p.optString("to"), p.optString("message"))
            "call" -> call(p.optString("to"))
            "location" -> location()
            "clipboard" -> clipboard(p.optString("text"))
            "press" -> a11y { if (it.press(p.optString("button", "home"))) "Done." else "That button isn't supported." }
            "tap_text" -> a11y { it.tapText(p.optString("text")) }
            "type_text" -> a11y { it.typeText(p.optString("text")) }
            "read_screen" -> a11y { it.readScreen().ifEmpty { "The screen has no readable text." } }
            "status" -> Res(true, status())
            else -> Res(false, error = "Unknown phone action '$action'.")
        }
    } catch (e: SecurityException) {
        Res(false, error = "Permission missing (${e.message}). Open the Habibi app → SETUP and allow it.")
    } catch (e: Exception) {
        Res(false, error = e.message ?: e.toString())
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private fun granted(perm: String) =
        ctx.checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED

    /** Android blocks apps in the background from opening screens unless
     *  "Display over other apps" is allowed for Habibi. */
    private fun startScreen(intent: Intent, okText: String): Res {
        if (Build.VERSION.SDK_INT >= 29 && !Settings.canDrawOverlays(ctx)) {
            return Res(false, error = "Allow 'Display over other apps' for Habibi (Habibi app → SETUP) so it can open things on the phone.")
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(intent)
        return Res(true, okText)
    }

    private fun a11y(block: (HabibiA11y) -> String): Res {
        val svc = HabibiA11y.instance
            ?: return Res(false, error = "Turn on Accessibility for Habibi (Habibi app → SETUP) to use this.")
        return Res(true, block(svc))
    }

    @Suppress("DEPRECATION")
    private fun launcherApps(): List<Pair<String, String>> {
        val pm = ctx.packageManager
        val i = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        return pm.queryIntentActivities(i, 0)
            .map { it.loadLabel(pm).toString() to it.activityInfo.packageName }
            .distinctBy { it.second }
    }

    // ── actions ──────────────────────────────────────────────────────────────

    private fun openApp(name: String): Res {
        val q = name.trim().lowercase()
        if (q.isEmpty()) return Res(false, error = "Which app?")
        if (q == "camera") return startScreen(Intent(MediaStore.INTENT_ACTION_STILL_IMAGE_CAMERA), "Opened the camera.")
        val apps = launcherApps()
        val hit = apps.firstOrNull { it.first.lowercase() == q }
            ?: apps.firstOrNull { it.first.lowercase().startsWith(q) }
            ?: apps.firstOrNull { it.first.lowercase().contains(q) }
            ?: apps.firstOrNull { q.contains(it.first.lowercase()) && it.first.length > 2 }
            ?: return Res(false, error = "No app called '$name' on the phone.")
        val launch = ctx.packageManager.getLaunchIntentForPackage(hit.second)
            ?: return Res(false, error = "${hit.first} can't be opened.")
        return startScreen(launch, "Opened ${hit.first}.")
    }

    private fun openUrl(raw: String): Res {
        var url = raw.trim()
        if (url.isEmpty()) return Res(false, error = "Which link?")
        if (!url.contains("://")) url = "https://$url"
        return startScreen(Intent(Intent.ACTION_VIEW, Uri.parse(url)), "Opened $url.")
    }

    private fun media(cmd: String): Res {
        val code = when (cmd.lowercase()) {
            "play" -> KeyEvent.KEYCODE_MEDIA_PLAY
            "pause" -> KeyEvent.KEYCODE_MEDIA_PAUSE
            "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous", "prev", "back" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
        }
        am.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        am.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
        return Res(true, "Media: $cmd.")
    }

    private fun volume(p: JSONObject): Res {
        val s = AudioManager.STREAM_MUSIC
        val max = am.getStreamMaxVolume(s)
        val level = p.optInt("level", -1)
        when {
            level in 0..100 -> am.setStreamVolume(s, Math.round(level * max / 100f), AudioManager.FLAG_SHOW_UI)
            p.optString("change") == "up" -> am.adjustStreamVolume(s, AudioManager.ADJUST_RAISE, AudioManager.FLAG_SHOW_UI)
            p.optString("change") == "down" -> am.adjustStreamVolume(s, AudioManager.ADJUST_LOWER, AudioManager.FLAG_SHOW_UI)
            p.optString("change") == "mute" -> am.adjustStreamVolume(s, AudioManager.ADJUST_TOGGLE_MUTE, AudioManager.FLAG_SHOW_UI)
        }
        val now = Math.round(am.getStreamVolume(s) * 100f / max)
        return Res(true, "Phone media volume is $now%.")
    }

    private fun flashlight(on: Boolean): Res {
        val cm = ctx.getSystemService(CameraManager::class.java)
        val id = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return Res(false, error = "This phone has no flashlight.")
        cm.setTorchMode(id, on)
        return Res(true, if (on) "Flashlight on." else "Flashlight off.")
    }

    private fun findPhone(): Res {
        am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        val tone = RingtoneManager.getRingtone(ctx, uri)
        tone?.audioAttributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).build()
        tone?.play()
        @Suppress("DEPRECATION")
        val vib = ctx.getSystemService(Vibrator::class.java)
        vib?.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 800, 400), 0))
        main.postDelayed({
            try { tone?.stop() } catch (_: Exception) {}
            try { vib?.cancel() } catch (_: Exception) {}
        }, 20_000)
        return Res(true, "The phone is ringing for 20 seconds.")
    }

    private fun battery(): JSONObject {
        val bm = ctx.getSystemService(BatteryManager::class.java)
        return JSONObject()
            .put("level_percent", bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY))
            .put("charging", bm.isCharging)
    }

    private fun notifications(): Res {
        if (!NotificationManagerCompat.getEnabledListenerPackages(ctx).contains(ctx.packageName)) {
            return Res(false, error = "Turn on Notification access for Habibi (Habibi app → SETUP).")
        }
        val list = NotifListener.recent.take(15)
        if (list.isEmpty()) return Res(true, "No recent notifications.")
        return Res(true, JSONArray(list))
    }

    /** A number as-is, or the first contact whose name matches. */
    private fun resolve(to: String): Pair<String, String>? {
        val cleaned = to.replace(Regex("[\\s()\\-]"), "")
        if (cleaned.matches(Regex("\\+?\\d{3,}"))) return cleaned to cleaned
        if (!granted(Manifest.permission.READ_CONTACTS)) throw SecurityException("contacts")
        val uri = ContactsContract.CommonDataKinds.Phone.CONTENT_URI
        val proj = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER
        )
        ctx.contentResolver.query(
            uri, proj, "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?",
            arrayOf("%${to.trim()}%"), null
        )?.use { c -> if (c.moveToFirst()) return c.getString(0) to c.getString(1) }
        return null
    }

    private fun sendSms(to: String, message: String): Res {
        if (to.isBlank() || message.isBlank()) return Res(false, error = "Need who to text and the message.")
        if (!granted(Manifest.permission.SEND_SMS)) throw SecurityException("SMS")
        val (name, number) = resolve(to) ?: return Res(false, error = "No contact called '$to'.")
        @Suppress("DEPRECATION")
        val sms = if (Build.VERSION.SDK_INT >= 31) ctx.getSystemService(SmsManager::class.java) else SmsManager.getDefault()
        sms.sendMultipartTextMessage(number, null, sms.divideMessage(message), null, null)
        return Res(true, "Text sent to $name ($number).")
    }

    private fun call(to: String): Res {
        if (to.isBlank()) return Res(false, error = "Who should I call?")
        if (!granted(Manifest.permission.CALL_PHONE)) throw SecurityException("phone calls")
        val (name, number) = resolve(to) ?: return Res(false, error = "No contact called '$to'.")
        return startScreen(Intent(Intent.ACTION_CALL, Uri.parse("tel:$number")), "Calling $name ($number).")
    }

    @SuppressLint("MissingPermission")
    private fun location(): Res {
        if (!granted(Manifest.permission.ACCESS_FINE_LOCATION) && !granted(Manifest.permission.ACCESS_COARSE_LOCATION)) {
            throw SecurityException("location")
        }
        val lm = ctx.getSystemService(LocationManager::class.java)
        var best: Location? = null
        if (Build.VERSION.SDK_INT >= 30) {
            val provider = when {
                lm.isProviderEnabled(LocationManager.GPS_PROVIDER) -> LocationManager.GPS_PROVIDER
                lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER) -> LocationManager.NETWORK_PROVIDER
                else -> null
            }
            if (provider != null) {
                val latch = CountDownLatch(1)
                val ex = Executors.newSingleThreadExecutor()
                lm.getCurrentLocation(provider, null, ex) { loc -> best = loc; latch.countDown() }
                latch.await(12, TimeUnit.SECONDS)
                ex.shutdown()
            }
        }
        if (best == null) {
            best = lm.getProviders(true).mapNotNull { lm.getLastKnownLocation(it) }.maxByOrNull { it.time }
        }
        val loc = best ?: return Res(false, error = "Location is unavailable — is Location turned on?")
        val ageS = (System.currentTimeMillis() - loc.time) / 1000
        return Res(true, JSONObject()
            .put("latitude", loc.latitude)
            .put("longitude", loc.longitude)
            .put("accuracy_m", loc.accuracy.toInt())
            .put("age_seconds", ageS)
            .put("map", "https://maps.google.com/?q=${loc.latitude},${loc.longitude}"))
    }

    private fun clipboard(text: String): Res {
        if (text.isEmpty()) return Res(false, error = "Nothing to copy.")
        val latch = CountDownLatch(1)
        main.post {
            ctx.getSystemService(ClipboardManager::class.java)
                .setPrimaryClip(ClipData.newPlainText("Habibi", text))
            latch.countDown()
        }
        latch.await(3, TimeUnit.SECONDS)
        return Res(true, "Copied to the phone's clipboard.")
    }

    private fun status(): JSONObject {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        val net = when {
            caps == null -> "offline"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "mobile data"
            else -> "other"
        }
        return JSONObject()
            .put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
            .put("android", Build.VERSION.RELEASE)
            .put("battery", battery())
            .put("network", net)
            .put("accessibility_on", HabibiA11y.instance != null)
            .put("notification_access", NotificationManagerCompat.getEnabledListenerPackages(ctx).contains(ctx.packageName))
            .put("can_open_screens", Build.VERSION.SDK_INT < 29 || Settings.canDrawOverlays(ctx))
    }
}
