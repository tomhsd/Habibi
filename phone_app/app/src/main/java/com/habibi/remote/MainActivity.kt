package com.habibi.remote

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/**
 * The whole app is the Habibi HUD:
 *  - not paired yet → animated pairing screen (assets/setup.html)
 *  - paired         → the live Habibi remote from the PC (reactor, waveform,
 *                     live listening, phone speaker), full screen.
 */
class MainActivity : Activity() {

    private var web: WebView? = null
    private var fileCb: ValueCallback<Array<Uri>>? = null
    private var mode = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        askBasicPermissions()
        if (Prefs.isPaired(this)) {
            HabibiService.start(this)
            showRemote()
        } else {
            showSetup()
        }
    }

    override fun onResume() {
        super.onResume()
        web?.onResume()
        if (mode == "remote" && !Prefs.isPaired(this)) showSetup()   // unpaired in SETUP
    }

    override fun onPause() {
        // Keep the page running (live listening keeps working while the screen is on).
        super.onPause()
    }

    override fun onDestroy() {
        web?.destroy()
        web = null
        super.onDestroy()
    }

    private fun askBasicPermissions() {
        val want = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) want.add(Manifest.permission.POST_NOTIFICATIONS)
        val missing = want.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 1)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun newWebView(): WebView {
        web?.destroy()
        val w = WebView(this)
        w.setBackgroundColor(Pal.BG)
        w.settings.javaScriptEnabled = true
        w.settings.domStorageEnabled = true
        w.settings.mediaPlaybackRequiresUserGesture = false   // Habibi's voice plays without a tap
        w.webViewClient = object : WebViewClient() {
            @SuppressLint("WebViewClientOnReceivedSslError")
            override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
                if (Prefs.selfSigned(this@MainActivity)) handler?.proceed() else handler?.cancel()
            }
        }
        w.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest?) {
                val req = request ?: return
                runOnUiThread {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        req.grant(req.resources)
                    } else {
                        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 2)
                        req.deny()
                    }
                }
            }

            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                fileCb?.onReceiveValue(null)
                fileCb = filePathCallback
                val intent = fileChooserParams?.createIntent()
                    ?: Intent(Intent.ACTION_GET_CONTENT).setType("*/*")
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                return try {
                    startActivityForResult(intent, 42)
                    true
                } catch (e: Exception) {
                    fileCb = null
                    false
                }
            }
        }
        setContentView(w)
        web = w
        return w
    }

    // ── pairing screen ───────────────────────────────────────────────────────

    private fun showSetup() {
        mode = "setup"
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val w = newWebView()
        w.addJavascriptInterface(SetupBridge(), "HabibiSetup")
        w.loadUrl("file:///android_asset/setup.html")
    }

    inner class SetupBridge {
        @JavascriptInterface fun savedServer(): String = Prefs.server(this@MainActivity)
        @JavascriptInterface fun savedSelfSigned(): Boolean = Prefs.selfSigned(this@MainActivity)
        @JavascriptInterface fun deviceName(): String = Build.MODEL ?: "Phone"

        @JavascriptInterface
        fun pair(serverRaw: String, pinRaw: String, nameRaw: String, trustSelf: Boolean) {
            val server = Net.normalize(serverRaw)
            val pin = pinRaw.trim().uppercase()
            val name = nameRaw.ifBlank { Build.MODEL ?: "Phone" }
            Thread {
                val problem: String? = try {
                    val body = JSONObject().put("pin", pin).put("name", name).toString()
                        .toRequestBody("application/json".toMediaType())
                    val req = Request.Builder().url("$server/api/pair").post(body).build()
                    Net.client(trustSelf).newCall(req).execute().use { r ->
                        val j = JSONObject(r.body?.string() ?: "{}")
                        if (j.optBoolean("ok")) {
                            Prefs.save(this@MainActivity, server, j.getString("device_token"), trustSelf)
                            null
                        } else {
                            j.optString("error", "Pairing failed (HTTP ${r.code}).")
                        }
                    }
                } catch (e: Exception) {
                    "Can't reach Habibi: ${e.message}"
                }
                runOnUiThread {
                    val ok = problem == null
                    web?.evaluateJavascript(
                        "window.onPair && window.onPair($ok, ${JSONObject.quote(problem ?: "")})", null
                    )
                    if (ok) {
                        HabibiService.start(this@MainActivity)
                        web?.postDelayed({ if (mode == "setup") showRemote() }, 1200)   // let "LINKED" glow
                    }
                }
            }.start()
        }
    }

    // ── the live Habibi remote ───────────────────────────────────────────────

    private fun showRemote() {
        mode = "remote"
        val w = newWebView()
        w.addJavascriptInterface(AppBridge(), "HabibiApp")
        w.loadUrl(Prefs.server(this) + "/app-login?device=" + Uri.encode(Prefs.token(this)))
    }

    /** Small, safe bridge for the remote page: no pairing secrets exposed. */
    inner class AppBridge {
        @JavascriptInterface
        fun linkStatus(): String = if (HabibiService.connected) "linked" else "connecting"

        @JavascriptInterface
        fun openSetup() {
            runOnUiThread { startActivity(Intent(this@MainActivity, SettingsActivity::class.java)) }
        }

        @JavascriptInterface
        fun keepAwake(on: Boolean) {
            runOnUiThread {
                if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 42) {
            fileCb?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            fileCb = null
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        val w = web
        if (mode == "remote" && w != null && w.canGoBack()) w.goBack() else super.onBackPressed()
    }
}
