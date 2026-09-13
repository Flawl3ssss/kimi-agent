package com.coomi.kimi

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewFeature
import java.io.File

/**
 * The whole app: a WebView on the agent's own console, plus a status strip that
 * reflects the runtime's real state (unpacking, starting, failed).
 *
 * Nothing else is native — sessions, prompts, permissions and settings are all
 * served by the Python agent, so the UI stays in web/ and this activity is just
 * a shell that keeps the process alive.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var status: TextView
    private lateinit var overlay: View

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        web = findViewById(R.id.web)
        status = findViewById(R.id.statusText)
        overlay = findViewById(R.id.overlay)

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = android.webkit.WebSettings.LOAD_NO_CACHE
            allowFileAccess = false
            allowContentAccess = false
            textZoom = 100
        }
        // The agent console is a dark UI; force the theme so WebView does not
        // invert it on some OEM builds.
        runCatching {
            if (WebViewFeature.isFeatureSupported(WebViewFeature.FORCE_DARK)) {
                WebSettingsCompat.setForceDark(
                    web.settings, WebSettingsCompat.FORCE_DARK_OFF
                )
            }
        }
        web.setBackgroundColor(Color.parseColor("#0f1115"))
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView?, request: WebResourceRequest?
            ): Boolean {
                val uri = request?.url ?: return false
                // Loopback stays inside the WebView; everything else is external.
                if (uri.host in setOf("127.0.0.1", "localhost", "[::1]")) return false
                runCatching {
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
                }
                return true
            }

            override fun onReceivedError(
                view: WebView?, request: WebResourceRequest?, error: android.webkit.WebResourceError?
            ) {
                if (request?.isForMainFrame == true) {
                    status.text = "Агент недоступен: ${error?.description}"
                    overlay.visibility = View.VISIBLE
                }
            }
        }

        findViewById<Button>(R.id.retryButton).setOnClickListener {
            overlay.visibility = View.GONE
            // Retry has to re-run the whole unpack+start path: after a failed
            // start there is no process to restart, and killing nothing would
            // leave the button permanently inert.
            AgentService.start(this@MainActivity)
            refresh()
        }
        findViewById<Button>(R.id.logButton).setOnClickListener { showLogs() }

        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        AgentService.start(this)
        pollStatus()
    }

    /** Poll the shared status object; the WebView reloads once the port opens. */
    private fun pollStatus() {
        status.postDelayed(object : Runnable {
            override fun run() {
                when (AgentStatus.phase) {
                    AgentStatus.PHASE_RUNNING -> {
                        overlay.visibility = View.GONE
                        status.text = "Агент работает · http://127.0.0.1:${RuntimeSpec.PORT}"
                        if (!loaded) {
                            web.loadUrl("http://127.0.0.1:${RuntimeSpec.PORT}/")
                            loaded = true
                        }
                    }
                    AgentStatus.PHASE_FAILED, AgentStatus.PHASE_EXITED -> {
                        overlay.visibility = View.VISIBLE
                        status.text = "Ошибка: ${AgentStatus.message}"
                    }
                    else -> {
                        overlay.visibility = View.VISIBLE
                        status.text = "${AgentStatus.message}"
                    }
                }
                status.postDelayed(this, 1000)
            }
        }, 500)
    }

    private var loaded = false

    private fun showLogs() {
        val txt = runCatching {
            File(filesDir, "logs/agent-service.log").readLines().takeLast(400).joinToString("\n")
        }.getOrElse { "нет лога: ${it.message}" }
        findViewById<TextView>(R.id.logText).text = txt
        findViewById<View>(R.id.logPanel).visibility = View.VISIBLE
        findViewById<Button>(R.id.closeLog).setOnClickListener {
            findViewById<View>(R.id.logPanel).visibility = View.GONE
        }
        findViewById<Button>(R.id.shareLog).setOnClickListener {
            val f = File(cacheDir, "agent-service.log")
            runCatching {
                f.writeText(txt)
                val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", f)
                startActivity(Intent.createChooser(
                    Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, uri)
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),
                    "Лог агента"
                ))
            }
        }
    }

    override fun onResume() {
        super.onResume()
        if (loaded) refresh()
    }

    /** Reload only when the page drifted from the agent (after a restart). */
    private fun refresh() {
        web.evaluateJavascript(
            "(function(){return document.readyState + ':' + (location.host||'')})()"
        ) { value ->
            if (value?.contains("127.0.0.1") != true) {
                web.loadUrl("http://127.0.0.1:${RuntimeSpec.PORT}/")
                loaded = true
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        web.destroy()
    }

    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }
}
