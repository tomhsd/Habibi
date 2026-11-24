package com.habibi.remote

import android.content.Context
import android.content.SharedPreferences
import okhttp3.OkHttpClient
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

/** Colours of the Windows HUD (ui.py class C). */
object Pal {
    const val BG = 0xFF00060A.toInt()
    const val PANEL = 0xFF010D14.toInt()
    const val CARD = 0xFF02121C.toInt()
    const val PRI = 0xFF00D4FF.toInt()
    const val DIM = 0xFF3A8A9A.toInt()
    const val TEXT = 0xFF8FFCFF.toInt()
    const val WHITE = 0xFFD8F8FF.toInt()
    const val GREEN = 0xFF00FF88.toInt()
    const val ACC2 = 0xFFFFCC00.toInt()
    const val RED = 0xFFFF3355.toInt()
}

/** Pairing data: the Habibi address and this phone's permanent device token. */
object Prefs {
    private fun sp(c: Context): SharedPreferences = c.getSharedPreferences("habibi", Context.MODE_PRIVATE)
    fun server(c: Context): String = sp(c).getString("server", "") ?: ""
    fun token(c: Context): String = sp(c).getString("token", "") ?: ""
    fun selfSigned(c: Context): Boolean = sp(c).getBoolean("self_signed", false)
    fun isPaired(c: Context): Boolean = server(c).isNotEmpty() && token(c).isNotEmpty()
    fun save(c: Context, server: String, token: String, selfSigned: Boolean) {
        sp(c).edit().putString("server", server).putString("token", token)
            .putBoolean("self_signed", selfSigned).apply()
    }
    fun clear(c: Context) { sp(c).edit().clear().apply() }
}

object Net {
    /** OkHttp client. With [selfSigned] it accepts the PC's own certificate
     *  (only needed without Tailscale, on home Wi-Fi). */
    fun client(selfSigned: Boolean): OkHttpClient {
        val b = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(20, TimeUnit.SECONDS)
        if (selfSigned) {
            val tm = object : X509TrustManager {
                override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
                override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
                override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
            }
            val ctx = SSLContext.getInstance("TLS")
            ctx.init(null, arrayOf<TrustManager>(tm), SecureRandom())
            b.sslSocketFactory(ctx.socketFactory, tm).hostnameVerifier { _, _ -> true }
        }
        return b.build()
    }

    fun normalize(url: String): String {
        var u = url.trim().trimEnd('/')
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://$u"
        return u
    }
}
