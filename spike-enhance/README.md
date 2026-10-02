# spike-enhance — 信号能不能救回来？

**⚠️ 一次性验证代码，不是产品。**

## 结论：救不回来

详见 [`RESULTS.md`](RESULTS.md)。摘要：

- 三步处理（高通 → 谱减降噪 → 高频补偿）把体检指标从「四项全不合格」
  改善到「两项合格」：SNR 16.9 → 29.4 dB，高频衰减 −27.6 → −20.7 dB
- **但 ASR 输出依然是不成句的乱码**，和原始录音一样
- 原因：辅音信息在**录音时**就丢了，不是被压低了。放大 8 dB 放大的是同频段噪声
- SNR 的改善是伪改善——降噪压低静音段，分位数差值自然变大，但语音可懂度没变

**信号损失不可逆。录音时麦克风没收到的高频，算法变不出来。**

这否定了 `spike-asr/analysis_report.md` 里「优先做降噪」「高频补偿」两条建议。
重录是唯一出路。

## 环境

复用 `spike-asr/.venv`（已有 numpy + sherpa-onnx）：

```powershell
..\spike-asr\.venv\Scripts\python.exe enhance.py <输入.wav> -o <输出.wav>
```

## 用法

```powershell
# 三步全做
..\spike-asr\.venv\Scripts\python.exe enhance.py 录音.wav -o 输出.wav

# 逐步存盘，便于量化每步贡献
..\spike-asr\.venv\Scripts\python.exe enhance.py 录音.wav --steps --outdir out

# 单独开关某一步
..\spike-asr\.venv\Scripts\python.exe enhance.py 录音.wav --no-denoise
```

## 三步各做什么

| 步骤 | 手段 | 参数 |
|---|---|---|
| 高通 | 频域滤波，余弦过渡带，零相位 | 100 Hz（过渡带 50-100） |
| 谱减降噪 | 噪声谱取最安静 10% 帧 | 过减 1.5，谱底 5% |
| 高频补偿 | 2k→4k 线性爬升，4k 以上满额 | +8 dB |

## 判据是 ASR 输出，不是指标

这个实验最重要的教训：**指标改善了 12 dB，转写一点没变好。**

`tools/record_check.py` 的指标是**筛查工具**——用来快速排除明显不合格的录音，
不是**验收工具**。指标合格不代表录音可用。最终判据永远是：
处理后的音频喂给 ASR，转出来的字成不成句。

## 为什么保留这个「失败」的探针

因为它排除了一条路。没有这个实验，「要不要先降噪试试」会一直是个悬而未决的选项，
每次讨论都要重新权衡。现在它是死的了。
```
