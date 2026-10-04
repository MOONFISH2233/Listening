package com.moonfish.listening

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log

/**
 * 麦克风采集。
 *
 * 参数固定为 16 kHz / 单声道 / PCM16，和仓库里已有的工具对齐：
 *  - spike-asr/run_asr.py 的 SAMPLE_RATE = 16000
 *  - tools/record_check.py 读的也是 16 bit 单声道 WAV
 *
 * 这样录出来的东西不用转码就能直接喂给那两个脚本。
 *
 * 为什么用 AudioRecord 而不是 MediaRecorder：MediaRecorder 只写文件，
 * 拿不到实时 PCM，而流式 ASR 必须边录边喂。
 */
class AudioCapture {

    companion object {
        const val TAG = "AudioCapture"
        const val SAMPLE_RATE = 16_000

        // 每次读 0.1 秒。和官方 sherpa-onnx demo 的 interval 一致。
        const val CHUNK_MS = 100
        const val CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS / 1000  // 1600
    }

    private var record: AudioRecord? = null

    /** 采集是否已经启动。 */
    @Volatile
    var isCapturing: Boolean = false
        private set

    /**
     * 打开麦克风。
     *
     * 调用方必须已经拿到 RECORD_AUDIO 权限——这里不申请，只检查，
     * 因为权限申请要 Activity 参与，不该塞进采集类。
     *
     * @return 打开成功返回 true
     */
    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (isCapturing) return true

        val minBytes = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBytes <= 0) {
            Log.e(TAG, "getMinBufferSize 返回 $minBytes，这个设备不支持 16k/mono/PCM16")
            return false
        }

        // 缓冲区给两倍最小值的余量：一次读 1600 个采样点（3200 字节），
        // 太小的 buffer 在系统忙的时候会丢帧。
        val bufferBytes = maxOf(minBytes * 2, CHUNK_SAMPLES * 2 * 2)

        val r = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufferBytes
        )

        if (r.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord 初始化失败，state=${r.state}")
            r.release()
            return false
        }

        record = r
        r.startRecording()
        if (r.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            Log.e(TAG, "startRecording 之后状态不是 RECORDING")
            r.release()
            record = null
            return false
        }

        isCapturing = true
        return true
    }

    /**
     * 读一块 PCM。阻塞，直到读满或出错。
     *
     * @param out 长度应为 CHUNK_SAMPLES 的 ShortArray，由调用方复用，避免长跑时反复分配
     * @return 实际读到的采样点数；<= 0 表示出错或已停止
     */
    fun read(out: ShortArray): Int {
        val r = record ?: return -1
        if (!isCapturing) return -1

        val n = r.read(out, 0, out.size)
        if (n < 0) {
            Log.e(TAG, "AudioRecord.read 返回 $n（ERROR_INVALID_OPERATION=-3 等）")
        }
        return n
    }

    /** 停止并释放麦克风。可以重复调用。 */
    fun stop() {
        isCapturing = false
        val r = record ?: return
        record = null
        try {
            if (r.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                r.stop()
            }
        } catch (e: IllegalStateException) {
            Log.w(TAG, "stop 时状态异常，忽略", e)
        } finally {
            r.release()
        }
    }
}
