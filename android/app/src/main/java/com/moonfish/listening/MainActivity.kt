package com.moonfish.listening

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.util.Log
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

/**
 * 唯一的一个界面。
 *
 * 它只做三件事：拿到麦克风权限、启停 [RecorderService]、把服务回吐的状态画出来。
 * 录音和识别全在服务里——这样锁屏、切后台、Activity 被回收都不影响录音。
 *
 * 关于「按下标记按钮之后发生什么」：
 *   1. 从最近 60 秒的逐字稿里切一段上下文（[Marker.context]），**先落盘**
 *   2. 再把这段上下文发给 DeepSeek，流式把回答追加到回答区
 *   3. 回答回来后再存一次盘
 *
 * 先落盘再发请求是有意的：网断了、Key 没配、模型抽风，学生标过的东西也不能丢。
 */
class MainActivity : AppCompatActivity(), RecorderService.Listener {

    companion object {
        const val TAG = "MainActivity"
        private const val REQ_RECORD_AUDIO = 100
        private const val REQ_NOTIFICATION = 101
    }

    private lateinit var tvTimer: TextView
    private lateinit var tvStatus: TextView
    private lateinit var tvTranscript: TextView
    private lateinit var tvAnswer: TextView
    private lateinit var tvMarkerCount: TextView
    private lateinit var btnToggle: Button

    private var service: RecorderService? = null
    private var bound = false

    /** 当前这次录音的名字（也是文件名主干）。未开始时为空。 */
    private var currentTitle: String = ""

    /** 本课的标记，内存里也留一份，避免每次按按钮都重读文件。 */
    private val markers = mutableListOf<Marker>()

    /** 已定稿的逐字稿，标记上下文从这里切。 */
    @Volatile
    private var finalizedText: String = ""

    private val llm by lazy { LlmClient(BuildConfig.DEEPSEEK_API_KEY) }

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            service = (binder as? RecorderService.LocalBinder)?.service()
            bound = true
            // 服务可能已经在录了（比如 Activity 被回收后重建），把监听接上。
            RecorderService.listener = this@MainActivity
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
            bound = false
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        tvTimer = findViewById(R.id.tvTimer)
        tvStatus = findViewById(R.id.tvStatus)
        tvTranscript = findViewById(R.id.tvTranscript)
        tvAnswer = findViewById(R.id.tvAnswer)
        tvMarkerCount = findViewById(R.id.tvMarkerCount)
        btnToggle = findViewById(R.id.btnToggle)

        btnToggle.setOnClickListener { onToggleClicked() }

        findViewById<Button>(R.id.btnNotUnderstand).setOnClickListener {
            onMark(Marker.TYPE_NOT_UNDERSTAND)
        }
        findViewById<Button>(R.id.btnQuestion).setOnClickListener {
            onMark(Marker.TYPE_QUESTION)
        }
        findViewById<Button>(R.id.btnImportant).setOnClickListener {
            onMark(Marker.TYPE_IMPORTANT)
        }

        if (!llm.isConfigured) {
            tvStatus.text = "提示：没配 DeepSeek API Key，标记只会存下来，不会出答案"
        }
    }

    override fun onStart() {
        super.onStart()
        // 绑定到服务。服务没在跑时 bindService 也会成功（只是 onStartCommand 没被调过），
        // 所以不能靠 bound 判断是否在录音，要看 service.running 之外的状态——
        // 这里用 currentTitle 是否为空 + 服务的回调来驱动 UI。
        bindService(
            Intent(this, RecorderService::class.java),
            connection,
            Context.BIND_AUTO_CREATE
        )
        RecorderService.listener = this
    }

    override fun onStop() {
        super.onStop()
        // 一定要摘掉监听，否则服务持有 Activity 会导致泄漏。
        if (RecorderService.listener === this) {
            RecorderService.listener = null
        }
        if (bound) {
            unbindService(connection)
            bound = false
        }
    }

    // ---- 启停 ----

    private fun onToggleClicked() {
        val s = service
        if (currentTitle.isNotEmpty()) {
            // 正在录 → 停
            startService(
                Intent(this, RecorderService::class.java).setAction(RecorderService.ACTION_STOP)
            )
            currentTitle = ""
            btnToggle.text = getString(R.string.start_recording)
            tvStatus.text = getString(R.string.status_idle)
            return
        }

        if (s == null) {
            Toast.makeText(this, "服务还没连上，稍等一下再按", Toast.LENGTH_SHORT).show()
            return
        }

        if (!hasAudioPermission()) {
            requestPermissions()
            return
        }
        requestNotificationPermissionIfNeeded()

        // 文件名用「日期_时间」，和电脑上的录音对得上，不用再记一次。
        currentTitle = RecorderService.newTitle()
        markers.clear()
        finalizedText = ""
        tvTranscript.text = ""
        tvAnswer.text = ""
        updateMarkerCount()

        btnToggle.text = getString(R.string.stop_recording)
        tvStatus.text = getString(R.string.status_preparing)

        startForegroundService(
            Intent(this, RecorderService::class.java)
                .setAction(RecorderService.ACTION_START)
                .putExtra(RecorderService.EXTRA_TITLE, currentTitle)
        )
    }

    // ---- 标记 ----

    private fun onMark(type: String) {
        if (currentTitle.isEmpty()) {
            Toast.makeText(this, "先开始录音再标记", Toast.LENGTH_SHORT).show()
            return
        }

        val context = recentContext()
        val at = service?.secondsRecorded() ?: 0.0

        val marker = Marker(
            type = type,
            atSeconds = at,
            createdAtMs = System.currentTimeMillis(),
            context = context,
        )

        // 1) 先落盘。这一步失败也要继续，但要让用户知道。
        markers.add(marker)
        val saved = MarkerStore.save(this, currentTitle, markers)
        if (!saved) {
            Toast.makeText(this, "标记没能存下来，存储可能有问题", Toast.LENGTH_LONG).show()
        }
        updateMarkerCount()

        // 2) 再问 AI。没配 Key 就到此为止——标记已经保住了。
        if (!llm.isConfigured) {
            tvAnswer.text = "（没配 DeepSeek API Key，这条标记只存了下来）"
            return
        }

        val index = markers.size - 1
        tvAnswer.text = "${Marker.label(type)} ${marker.timeLabel()}\n\n"

        lifecycleScope.launch {
            val full = llm.ask(
                context = context,
                markerType = type,
                userQuestion = null,
            ) { delta ->
                // 回调在 IO 线程，切主线程追加。
                runOnUiThread {
                    tvAnswer.append(delta)
                }
            }

            // 3) 回答回来后写回盘。写回失败不影响已经显示的内容。
            if (full.isNotBlank()) {
                markers[index] = markers[index].copy(answer = full)
                MarkerStore.save(this@MainActivity, currentTitle, markers)
            } else {
                tvAnswer.append("\n（没拿到回答，检查网络或 API Key）")
            }
        }
    }

    /**
     * 取最近一段逐字稿当上下文。
     *
     * 按字符数粗切，不按时间——逐字稿里没有逐句的时间戳。
     * 中文一分钟大概 200~300 字，这里取 400 字作为近似。
     * 宁可多给一点：模型看到更多上下文不会变差，少了才会答偏。
     */
    private fun recentContext(): String {
        val all = finalizedText.trim()
        if (all.isEmpty()) return ""
        val max = 400
        return if (all.length <= max) all else all.substring(all.length - max)
    }

    private fun updateMarkerCount() {
        tvMarkerCount.text = if (markers.isEmpty()) {
            ""
        } else {
            val byType = markers.groupingBy { Marker.label(it.type) }.eachCount()
            "本课标记 ${markers.size} 个：" +
                byType.entries.joinToString("、") { "${it.key} ${it.value}" }
        }
    }

    // ---- 权限 ----

    private fun hasAudioPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    private fun requestPermissions() {
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.RECORD_AUDIO),
            REQ_RECORD_AUDIO
        )
    }

    /** Android 13 起，常驻通知也要用户同意；不给的话服务照样能跑，只是看不见通知。 */
    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val ok = ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        if (!ok) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                REQ_NOTIFICATION
            )
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_RECORD_AUDIO) {
            if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
                onToggleClicked()   // 拿到权限了，用户本来的意图就是开始录
            } else {
                tvStatus.text = getString(R.string.need_permission)
                Toast.makeText(this, R.string.need_permission, Toast.LENGTH_LONG).show()
            }
        }
    }

    // ---- RecorderService.Listener ----
    // 全部在录音线程上被调用，必须切主线程再碰 View。

    override fun onTick(seconds: Double) {
        runOnUiThread {
            val total = seconds.toInt()
            tvTimer.text = String.format(
                Locale.US, "%02d:%02d:%02d",
                total / 3600, (total % 3600) / 60, total % 60
            )
            if (tvStatus.text != getString(R.string.status_recording)) {
                tvStatus.text = getString(R.string.status_recording)
            }
        }
    }

    override fun onTranscript(finalized: String, pending: String) {
        finalizedText = finalized
        runOnUiThread {
            // 已定稿的正文 + 灰色的半句。半句用 HTML 上色最简单，不用两套 TextView。
            tvTranscript.text = finalized + pending
            scrollTranscriptToBottom()
        }
    }

    override fun onError(message: String) {
        runOnUiThread {
            tvStatus.text = message
            Toast.makeText(this, message, Toast.LENGTH_LONG).show()
        }
    }

    private fun scrollTranscriptToBottom() {
        findViewById<android.widget.ScrollView>(R.id.scrollTranscript)
            .post { it.fullScroll(android.view.View.FOCUS_DOWN) }
    }
}
