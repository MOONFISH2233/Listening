package com.moonfish.listening

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 录音前台服务。
 *
 * 为什么必须做成前台服务：Android 9 之后，应用退到后台就不能再访问麦克风，
 * 系统会在几分钟内掐掉录音。挂一个常驻通知（前台服务）是唯一被官方支持的
 * 长时录音方式。Android 14 起还要求声明 foregroundServiceType="microphone"
 * 和 FOREGROUND_SERVICE_MICROPHONE 权限，否则启动就崩。
 *
 * 这里同时持有一个 PARTIAL_WAKE_LOCK：屏幕关掉后 CPU 仍要转，否则录音线程
 * 会被挂起，录出来的音频会有大片空洞。
 *
 * 采集和落盘都在这个服务里，MainActivity 只做界面和启停，这样界面被销毁
 * 也不影响录音。
 */
class RecorderService : Service() {

    companion object {
        const val TAG = "RecorderService"

        const val ACTION_START = "com.moonfish.listening.START"
        const val ACTION_STOP = "com.moonfish.listening.STOP"

        const val EXTRA_TITLE = "title"

        private const val CHANNEL_ID = "tingke_recording"
        private const val NOTIFICATION_ID = 1001

        /** 录音存放目录：/sdcard/Android/data/<包名>/files/recordings */
        fun recordingsDir(ctx: Context): File =
            File(ctx.getExternalFilesDir(null), "recordings")

        /**
         * 生成这次录音的名字：yyyyMMdd_HHmmss。
         *
         * 界面在 start 之前就要拿到它（要显示、要落标记文件），
         * 所以由界面生成再通过 EXTRA_TITLE 传进来。服务自己也用同一个
         * 规则生成一份兜底值，两边格式必须一致。
         */
        fun newTitle(): String =
            SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())

        /** 界面上要显示的实时状态，服务通过它回吐给 Activity。 */
        @Volatile
        var listener: Listener? = null
    }

    /** 服务 → 界面的回调。全部在录音线程上触发，界面自己切主线程。 */
    interface Listener {
        /** 已录时长（秒） */
        fun onTick(seconds: Double)
        /** 逐字稿更新：finalized 是已定稿的全文，pending 是当前正在识别的半句 */
        fun onTranscript(finalized: String, pending: String)
        /** 出错 */
        fun onError(message: String)
    }

    inner class LocalBinder : Binder() {
        fun service(): RecorderService = this@RecorderService
    }

    private val binder = LocalBinder()

    private var capture: AudioCapture? = null
    private var writer: WavWriter? = null
    private var asr: AsrEngine? = null
    private var worker: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null

    @Volatile
    private var running = false

    /** 已定稿的逐字稿（endpoint 之后的句子）。 */
    private val finalized = StringBuilder()

    private var startedAtMs = 0L

    /** 当前这次录音的文件名主干，界面拿它拼标记文件名。 */
    @Volatile
    var currentTitle: String = ""
        private set

    /**
     * 已录时长（秒）。界面在按下标记按钮时调用，用来记时间点。
     * 没在录时返回 0。
     */
    fun secondsRecorded(): Double = writer?.secondsRecorded() ?: 0.0

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                val title = intent.getStringExtra(EXTRA_TITLE) ?: defaultTitle()
                startRecording(title)
            }
            ACTION_STOP -> stopRecording()
        }
        // 被系统杀掉后不要自动重启：重启时麦克风权限状态未知，
        // 而且用户会看到一个没人操作却在录音的 app。
        return START_NOT_STICKY
    }

    private fun defaultTitle(): String =
        SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())

    private fun startRecording(title: String) {
        if (running) return

        currentTitle = title
        startForegroundSafely(title)
        acquireWakeLock()

        val dir = recordingsDir(this)
        val wav = File(dir, "$title.wav")
        val w = WavWriter(wav)
        if (!w.open()) {
            notifyError("打不开录音文件：${wav.absolutePath}")
            stopSelf()
            return
        }
        writer = w

        val c = AudioCapture()
        if (!c.start()) {
            notifyError("麦克风打不开，可能是权限没给或被别的应用占用")
            w.close()
            stopSelf()
            return
        }
        capture = c

        // ASR 失败不该拖垮录音：转不出字，录音文件仍然是有价值的。
        asr = try {
            AsrEngine(this).also { it.init() }
        } catch (e: Exception) {
            Log.e(TAG, "ASR 初始化失败，本次只录音不转写", e)
            notifyError("识别引擎没起来，本次只录音（${e.message}）")
            null
        }

        startedAtMs = System.currentTimeMillis()
        running = true

        worker = Thread({ loop() }, "tingke-recorder").apply {
            priority = Thread.MAX_PRIORITY
            start()
        }
    }

    /**
     * 采集主循环。
     *
     * 一次循环：读 0.1 秒 PCM → 写文件 → 喂给 ASR → 取结果 → 回调界面。
     * 写文件和 ASR 都在同一个线程里顺序做，不用加锁——这也意味着
     * **如果 ASR 单次解码慢过 0.1 秒，就会开始丢帧**。第一版先观察，
     * 真有这个问题再拆线程。
     */
    private fun loop() {
        val c = capture ?: return
        val w = writer ?: return
        val buffer = ShortArray(AudioCapture.CHUNK_SAMPLES)

        var lastFlushMs = System.currentTimeMillis()
        var lastTickMs = 0L

        while (running) {
            val n = c.read(buffer)
            if (n <= 0) {
                if (running) Log.w(TAG, "read 返回 $n，采集可能中断")
                break
            }

            // 1) 落盘：优先保证录音完整，它比转写重要。
            if (!w.write(buffer, n)) {
                notifyError("写文件失败，可能是存储满了")
                break
            }

            // 2) 识别：失败只记日志，不影响录音。
            try {
                // 取局部 val：asr 是可空可变属性，直接连着调编译器
                // 无法智能转换（可能被别的线程改掉）。
                val engine = asr
                if (engine != null) {
                    engine.accept(buffer, n)
                    val pending = engine.currentText()
                    if (engine.isEndpoint()) {
                        if (pending.isNotBlank()) {
                            finalized.append(pending).append('\n')
                        }
                        engine.reset()
                    }
                    notifyTranscript(pending)
                }
            } catch (e: Exception) {
                Log.e(TAG, "识别出错，跳过这一块", e)
            }

            // 3) 每 10 秒把头和 fsync 刷一次：掉电最多损失 10 秒，
            //    而且头里的长度始终是对的。
            val now = System.currentTimeMillis()
            if (now - lastFlushMs >= 10_000) {
                w.flush()
                lastFlushMs = now
            }

            // 4) 计时每秒回调一次，别每 0.1 秒都刷界面。
            if (now - lastTickMs >= 1_000) {
                notifyTick(w.secondsRecorded())
                lastTickMs = now
            }
        }

        cleanup()
    }

    private fun stopRecording() {
        if (!running) {
            stopSelf()
            return
        }
        running = false
        worker?.join(2_000)
        worker = null
        stopSelf()
    }

    /** 线程退出或服务停止时的收尾，保证文件被正确关闭。 */
    private fun cleanup() {
        try {
            capture?.stop()
        } catch (e: Exception) {
            Log.w(TAG, "停止采集出错", e)
        }
        capture = null

        try {
            asr?.release()
        } catch (e: Exception) {
            Log.w(TAG, "释放识别引擎出错", e)
        }
        asr = null

        try {
            writer?.close()
        } catch (e: Exception) {
            Log.w(TAG, "关闭文件出错", e)
        }
        writer = null

        releaseWakeLock()
        running = false

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
    }

    override fun onDestroy() {
        running = false
        cleanup()
        super.onDestroy()
    }

    // ---- 通知与唤醒锁 ----

    private fun startForegroundSafely(title: String) {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                CHANNEL_ID,
                "录音",
                NotificationManager.IMPORTANCE_LOW   // LOW：不响不震，但常驻
            ).apply { description = "上课录音时常驻" }
            nm.createNotificationChannel(ch)
        }

        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, RecorderService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val n = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("听刻正在录音")
            .setContentText(title)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "停止", stop)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, n,
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "tingke:recording").apply {
            setReferenceCounted(false)
            acquire(6 * 60 * 60 * 1000L)   // 上限 6 小时，防忘关
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.let { if (it.isHeld) it.release() }
        } catch (e: Exception) {
            Log.w(TAG, "释放唤醒锁出错", e)
        }
        wakeLock = null
    }

    // ---- 回调界面 ----

    private fun notifyTick(seconds: Double) {
        listener?.onTick(seconds)
    }

    private fun notifyTranscript(pending: String) {
        listener?.onTranscript(finalized.toString(), pending)
    }

    private fun notifyError(msg: String) {
        Log.e(TAG, msg)
        listener?.onError(msg)
    }
}
