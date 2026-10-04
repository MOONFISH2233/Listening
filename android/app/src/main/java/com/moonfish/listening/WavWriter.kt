package com.moonfish.listening

import android.util.Log
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * 边录边写 WAV。
 *
 * 设计要点：**WAV 头先占位，每次 flush 时回填真实长度。**
 *
 * 为什么这么做：WAV 头里的 RIFF size 和 data size 必须写在实际数据前面，
 * 但录音开始时我们不知道会录多久。两种办法：
 *  1. 先在内存里攒满再写 —— 一小时 16k/mono/PCM16 是 115 MB，不能这么干
 *  2. 先占位、边写边回填 —— 就是这里用的
 *
 * 好处：**进程被系统杀掉、手机没电，文件也是完整可读的 WAV**。
 * 这一点对「录满一节课」这个目标很关键。
 *
 * 注意：这里不做重采样、不做增益，写进去的就是麦克风原始 PCM。
 * 音质问题留给采集方式和后续处理，写入端不引入任何失真。
 */
class WavWriter(private val file: File) {

    companion object {
        const val TAG = "WavWriter"
        private const val HEADER_BYTES = 44

        /**
         * 修复一个被强杀留下的 WAV：按文件实际大小回填头。
         *
         * 正常情况下用不到（每次 flush 都会写头），但如果进程被杀在
         * 两次 flush 之间，头里的长度会偏小，播放器会截断。启动时对所有
         * 录音文件跑一遍，保证「录到的东西一定拿得回来」。
         */
        fun repair(file: File): Boolean {
            return try {
                RandomAccessFile(file, "rw").use { f ->
                    val real = f.length() - HEADER_BYTES
                    if (real <= 0) return false
                    writeHeader(f, real)
                }
                true
            } catch (e: Exception) {
                Log.e(TAG, "修复 ${file.name} 失败", e)
                false
            }
        }

        /**
         * 把 44 字节的标准 WAV 头写到文件开头。
         *
         * 写完把文件指针留在数据末尾，方便继续追加。
         */
        private fun writeHeader(f: RandomAccessFile, dataBytes: Long) {
            val sampleRate = AudioCapture.SAMPLE_RATE
            val channels = 1
            val bitsPerSample = 16
            val byteRate = sampleRate * channels * bitsPerSample / 8
            val blockAlign = channels * bitsPerSample / 8

            val h = ByteBuffer.allocate(HEADER_BYTES).order(ByteOrder.LITTLE_ENDIAN)
            h.put("RIFF".toByteArray(Charsets.US_ASCII))
            h.putInt((36 + dataBytes).toInt())
            h.put("WAVE".toByteArray(Charsets.US_ASCII))
            h.put("fmt ".toByteArray(Charsets.US_ASCII))
            h.putInt(16)                    // fmt chunk 长度
            h.putShort(1)                   // PCM
            h.putShort(channels.toShort())
            h.putInt(sampleRate)
            h.putInt(byteRate)
            h.putShort(blockAlign.toShort())
            h.putShort(bitsPerSample.toShort())
            h.put("data".toByteArray(Charsets.US_ASCII))
            h.putInt(dataBytes.toInt())

            f.seek(0)
            f.write(h.array())
            f.seek(HEADER_BYTES + dataBytes)   // 回到末尾，继续追加
        }
    }

    private var raf: RandomAccessFile? = null
    private val pcmBuffer = ByteBuffer.allocate(AudioCapture.CHUNK_SAMPLES * 2)
        .order(ByteOrder.LITTLE_ENDIAN)

    /** 已写入的 PCM 字节数（不含头）。 */
    var bytesWritten: Long = 0
        private set

    val isOpen: Boolean get() = raf != null

    /** 创建文件并写下占位头。 */
    fun open(): Boolean {
        return try {
            file.parentFile?.mkdirs()
            val f = RandomAccessFile(file, "rw")
            f.setLength(0)
            f.write(ByteArray(HEADER_BYTES))   // 占位，边写边回填
            raf = f
            bytesWritten = 0
            true
        } catch (e: Exception) {
            Log.e(TAG, "打不开 ${file.absolutePath}", e)
            false
        }
    }

    /**
     * 追加一块 PCM。
     *
     * @param samples 采样点
     * @param count   实际有效的采样点数（可能小于数组长度）
     */
    fun write(samples: ShortArray, count: Int): Boolean {
        val f = raf ?: return false
        if (count <= 0) return true

        return try {
            pcmBuffer.clear()
            for (i in 0 until count) {
                pcmBuffer.putShort(samples[i])
            }
            f.write(pcmBuffer.array(), 0, count * 2)
            bytesWritten += count * 2
            true
        } catch (e: Exception) {
            Log.e(TAG, "写入失败，已写 $bytesWritten 字节", e)
            false
        }
    }

    /** 回填头、关闭文件。可以重复调用。 */
    fun close() {
        val f = raf ?: return
        raf = null
        try {
            writeHeader(f, bytesWritten)
            f.close()
        } catch (e: Exception) {
            Log.e(TAG, "关闭时出错", e)
        }
    }

    /** 落盘但不关闭。长录音时定期调用（比如每 10 秒），减少断电损失。 */
    fun flush() {
        val f = raf ?: return
        try {
            writeHeader(f, bytesWritten)
            f.fd.sync()
        } catch (e: Exception) {
            Log.w(TAG, "flush 失败", e)
        }
    }

    /** 已录时长（秒），界面上显示计时用。 */
    fun secondsRecorded(): Double = bytesWritten.toDouble() / (AudioCapture.SAMPLE_RATE * 2)
}
