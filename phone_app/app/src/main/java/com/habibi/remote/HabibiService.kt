package com.habibi.remote

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.Executors

/**
 * Keeps a secure WebSocket open to Habibi (/ws/device) and runs the commands
 * Habibi sends. Runs as a foreground service so Android does not kill it.
 */
class HabibiService : Service() {

    companion object {
        @Volatile var connected = false
        private const val CHANNEL = "habibi_link"
        private const val NOTIF_ID = 7

        fun start(c: Context) {
            val i = Intent(c, HabibiService::class.java)
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i) else c.startService(i)
        }

        fun stop(c: Context) { c.stopService(Intent(c, HabibiService::class.java)) }
    }

    private val main = Handler(Looper.getMainLooper())
    private val pool = Executors.newCachedThreadPool()
    private lateinit var exec: CommandExecutor
    private var ws: WebSocket? = null
    private var stopped = false
    private var backoff = 2000L
    private var retryPending = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        exec = CommandExecutor(this)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Habibi link", NotificationManager.IMPORTANCE_LOW)
        )
        val n = notif("Connecting to Habibi…")
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, n)
        }
        connect()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        stopped = true
        connected = false
        main.removeCallbacksAndMessages(null)
        try { ws?.close(1000, "bye") } catch (_: Exception) {}
        pool.shutdownNow()
        super.onDestroy()
    }

    private fun notif(text: String): Notification {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_habibi)
            .setContentTitle("Habibi")
            .setContentText(text)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
    }

    private fun setStatus(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notif(text))
    }

    private fun connect() {
        retryPending = false
        if (stopped) return
        if (!Prefs.isPaired(this)) { stopSelf(); return }
        val base = Prefs.server(this)
            .replaceFirst("https://", "wss://")
            .replaceFirst("http://", "ws://")
        val url = base + "/ws/device?device=" + URLEncoder.encode(Prefs.token(this), "UTF-8")
        val req = Request.Builder().url(url).build()
        ws = Net.client(Prefs.selfSigned(this)).newWebSocket(req, listener)
    }

    private fun scheduleRetry() {
        if (stopped || retryPending) return
        retryPending = true
        main.postDelayed({ connect() }, backoff)
        backoff = (backoff * 2).coerceAtMost(30_000L)
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            connected = true
            backoff = 2000L
            setStatus("Connected — Habibi can control this phone")
            val info = JSONObject()
                .put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
                .put("android", Build.VERSION.RELEASE)
            webSocket.send(JSONObject().put("type", "hello").put("info", info).toString())
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val msg = try { JSONObject(text) } catch (e: Exception) { return }
            if (msg.optString("type") != "cmd") return
            val id = msg.optString("id")
            val action = msg.optString("action")
            val params = msg.optJSONObject("params") ?: JSONObject()
            pool.execute {
                val r = exec.run(action, params)
                val out = JSONObject().put("type", "result").put("id", id).put("ok", r.ok)
                if (r.result != null) out.put("result", JSONObject.wrap(r.result))
                if (r.error != null) out.put("error", r.error)
                webSocket.send(out.toString())
            }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            connected = false
            if (code == 4001) setStatus("Not paired any more — open Habibi to pair again")
            else setStatus("Reconnecting…")
            scheduleRetry()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            connected = false
            setStatus("Can't reach Habibi — retrying (is the PC on?)")
            scheduleRetry()
        }
    }
}
