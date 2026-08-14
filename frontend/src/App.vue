<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import {
  ApiError,
  animatePortrait,
  connectLocalApi,
  defaultApiOrigin,
  getMotions,
  resolveApiUrl,
  restorePhoto,
  type ModelStatus,
  type MotionResponse,
} from "./api";

type ConnectionState = "checking" | "online" | "offline";
type ProcessingState = "idle" | "processing" | "complete" | "error";
type AnimationState = "idle" | "processing" | "complete" | "error";
type MotionOption = MotionResponse["items"][number];

const scaleLevels = [
  { value: 1, label: "轻度", position: "0%" },
  { value: 2, label: "标准", position: "33.333%" },
  { value: 3, label: "清晰", position: "66.667%" },
  { value: 4, label: "极致", position: "100%" },
] as const;

const maxUploadBytes = 15 * 1024 * 1024;
const maxDrivingVideoBytes = 64 * 1024 * 1024;
const acceptedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const acceptedDrivingTypes = new Set(["video/mp4", "video/quicktime", "video/webm"]);
const acceptedDrivingExtensions = new Set(["mp4", "mov", "webm"]);
const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
const apiOrigin = ref(defaultApiOrigin());

const connectionState = ref<ConnectionState>("checking");
const processingState = ref<ProcessingState>("idle");
const animationState = ref<AnimationState>("idle");
const model = ref<ModelStatus | null>(null);
const animation = ref<ModelStatus | null>(null);
const motionOptions = ref<MotionOption[]>([]);
const selectedMotion = ref<string | null>(null);
const customDrivingVideo = ref<File | null>(null);
const customDrivingPreviewUrl = ref("");
const selectedFile = ref<File | null>(null);
const localPreviewUrl = ref("");
const sourceUrl = ref("");
const resultUrl = ref("");
const animationResultUrl = ref("");
const restoreErrorMessage = ref("");
const animationErrorMessage = ref("");
const restoreStatusMessage = ref("正在检查本地模型服务…");
const animationStatusMessage = ref("正在检查动态模型服务…");
const scale = ref(2);
const dragActive = ref(false);
const consentConfirmed = ref(false);

const hasConnection = computed(() => connectionState.value === "online");
const modelLabel = computed(() => {
  if (!model.value) return "正在连接";
  if (!model.value.ready) return "模型待配置";
  return model.value.mode === "gfpgan" ? "GFPGAN 已就绪" : "预览模式";
});
const modelTone = computed(() => {
  if (!model.value?.ready || connectionState.value === "offline") return "warning";
  return model.value.mode === "gfpgan" ? "success" : "neutral";
});
const animationLabel = computed(() => {
  if (!animation.value) return "动态模型未连接";
  return animation.value.ready ? "LivePortrait 已就绪" : "动态模型待配置";
});
const animationTone = computed(() => {
  if (!animation.value?.ready || connectionState.value === "offline") return "warning";
  return "success";
});
const processButtonLabel = computed(() => {
  if (processingState.value === "processing") return "正在处理…";
  if (!hasConnection.value) return "连接本地服务后开始";
  if (model.value?.mode === "preview") return "生成预览结果";
  return "开始修复照片";
});
const canProcess = computed(
  () =>
    Boolean(selectedFile.value) &&
    hasConnection.value &&
    Boolean(model.value?.ready),
);
const canAnimate = computed(
  () =>
    Boolean(selectedFile.value) &&
    Boolean(selectedMotion.value || customDrivingVideo.value) &&
    consentConfirmed.value &&
    hasConnection.value &&
    Boolean(animation.value?.ready),
);
const animationButtonLabel = computed(() => {
  if (animationState.value === "processing") return "正在生成动态短片…";
  if (!hasConnection.value) return "连接本地服务后生成";
  if (!animation.value?.ready) return "动态模型待配置";
  if (!consentConfirmed.value) return "确认授权后生成";
  return "生成动态短片";
});
const selectedMotionOption = computed(
  () => motionOptions.value.find((motion) => motion.id === selectedMotion.value) ?? null,
);
const selectedDrivingLabel = computed(() => {
  if (customDrivingVideo.value) return `自定义视频 · ${customDrivingVideo.value.name}`;
  return selectedMotionOption.value?.label || "尚未选择驱动视频";
});
const activeScaleLevel = computed(
  () => scaleLevels.find((level) => level.value === scale.value) ?? scaleLevels[1],
);
const scaleProgress = computed(() => `${((scale.value - 1) / (scaleLevels.length - 1)) * 100}%`);

function releasePreview() {
  if (localPreviewUrl.value) URL.revokeObjectURL(localPreviewUrl.value);
}

function releaseCustomDrivingPreview() {
  if (customDrivingPreviewUrl.value) URL.revokeObjectURL(customDrivingPreviewUrl.value);
}

function clearCustomDrivingVideo() {
  releaseCustomDrivingPreview();
  customDrivingVideo.value = null;
  customDrivingPreviewUrl.value = "";
}

function selectFile(file: File | undefined) {
  if (!file) return;
  if (!acceptedTypes.has(file.type)) {
    restoreErrorMessage.value = "请选择 JPG、PNG 或 WebP 格式的图片。";
    processingState.value = "error";
    return;
  }
  if (file.size > maxUploadBytes) {
    restoreErrorMessage.value = "图片大小不能超过 15 MB。";
    processingState.value = "error";
    return;
  }

  releasePreview();
  selectedFile.value = file;
  localPreviewUrl.value = URL.createObjectURL(file);
  sourceUrl.value = "";
  resultUrl.value = "";
  restoreErrorMessage.value = "";
  animationErrorMessage.value = "";
  processingState.value = "idle";
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  selectFile(input.files?.[0]);
  input.value = "";
}

function onDrop(event: DragEvent) {
  dragActive.value = false;
  selectFile(event.dataTransfer?.files[0]);
}

function selectSampleMotion(motionId: string) {
  clearCustomDrivingVideo();
  selectedMotion.value = motionId;
  animationErrorMessage.value = "";
  animationResultUrl.value = "";
  animationState.value = "idle";
}

function isSupportedDrivingVideo(file: File) {
  const extension = file.name.split(".").pop()?.toLowerCase() || "";
  return acceptedDrivingTypes.has(file.type) || acceptedDrivingExtensions.has(extension);
}

function selectCustomDrivingVideo(file: File | undefined) {
  if (!file) return;
  if (!isSupportedDrivingVideo(file)) {
    animationErrorMessage.value = "请选择 MP4、MOV 或 WebM 格式的驱动视频。";
    animationState.value = "error";
    return;
  }
  if (file.size > maxDrivingVideoBytes) {
    animationErrorMessage.value = "驱动视频大小不能超过 64 MB。";
    animationState.value = "error";
    return;
  }

  clearCustomDrivingVideo();
  customDrivingVideo.value = file;
  customDrivingPreviewUrl.value = URL.createObjectURL(file);
  selectedMotion.value = null;
  animationErrorMessage.value = "";
  animationResultUrl.value = "";
  animationState.value = "idle";
}

function onCustomDrivingVideoChange(event: Event) {
  const input = event.target as HTMLInputElement;
  selectCustomDrivingVideo(input.files?.[0]);
  input.value = "";
}

function motionPreviewFromEvent(event: Event) {
  const target = event.currentTarget;
  if (target instanceof HTMLVideoElement) return target;
  return target instanceof HTMLElement ? target.querySelector<HTMLVideoElement>("video") : null;
}

function playMotionPreview(event: Event) {
  if (prefersReducedMotion) return;
  const video = motionPreviewFromEvent(event);
  if (!video) return;
  video.muted = true;
  void video.play().catch(() => undefined);
}

function stopMotionPreview(event: Event) {
  const video = motionPreviewFromEvent(event);
  if (!video) return;
  video.pause();
  video.currentTime = 0;
}

async function refreshConnection() {
  connectionState.value = "checking";
  try {
    const connection = await connectLocalApi();
    const { origin, health } = connection;
    const motions = await getMotions({ origin });
    apiOrigin.value = origin;
    model.value = health.model;
    animation.value = health.animation ?? null;
    motionOptions.value = motions.items.map((motion) => ({
      ...motion,
      previewUrl: resolveApiUrl(motion.previewUrl, origin),
    }));
    if (!customDrivingVideo.value) {
      const selectedIsStillAvailable = motionOptions.value.some((motion) => motion.id === selectedMotion.value);
      selectedMotion.value = selectedIsStillAvailable ? selectedMotion.value : motionOptions.value[0]?.id ?? null;
    }
    connectionState.value = "online";
    restoreStatusMessage.value = health.model.message || "本地服务已连接。";
    animationStatusMessage.value = health.animation?.message || "动态模型服务待配置。";
  } catch {
    connectionState.value = "offline";
    model.value = null;
    animation.value = null;
    restoreStatusMessage.value = "尚未连接本地模型服务；你仍可预览界面与图片。";
    animationStatusMessage.value = "尚未连接本地动态模型服务。";
  }
}

async function processPhoto() {
  if (!selectedFile.value || !canProcess.value) return;

  processingState.value = "processing";
  restoreErrorMessage.value = "";
  try {
    const response = await restorePhoto(selectedFile.value, scale.value, { origin: apiOrigin.value });
    sourceUrl.value = resolveApiUrl(response.sourceUrl || "", apiOrigin.value);
    resultUrl.value = resolveApiUrl(response.resultUrl, apiOrigin.value);
    model.value = response.model;
    restoreStatusMessage.value = response.message || "处理完成。";
    processingState.value = "complete";
  } catch (error) {
    processingState.value = "error";
    restoreErrorMessage.value =
      error instanceof ApiError
        ? error.message
        : "无法连接本地服务，请确认后端正在运行。";
  }
}

async function generateAnimation() {
  if (!selectedFile.value || !canAnimate.value) return;

  animationState.value = "processing";
  animationErrorMessage.value = "";
  try {
    const driver = customDrivingVideo.value
      ? { drivingVideo: customDrivingVideo.value }
      : { motionId: selectedMotion.value as string };
    const response = await animatePortrait(selectedFile.value, driver, { origin: apiOrigin.value });
    animation.value = response.animation;
    animationResultUrl.value = resolveApiUrl(response.resultUrl, apiOrigin.value);
    animationStatusMessage.value = response.message || "动态短片已生成。";
    animationState.value = "complete";
  } catch (error) {
    animationState.value = "error";
    animationErrorMessage.value =
      error instanceof ApiError
        ? error.message
        : "无法生成动态短片，请确认本地动态模型服务正在运行。";
  }
}

function resetWorkspace() {
  releasePreview();
  selectedFile.value = null;
  localPreviewUrl.value = "";
  sourceUrl.value = "";
  resultUrl.value = "";
  animationResultUrl.value = "";
  restoreErrorMessage.value = "";
  animationErrorMessage.value = "";
  processingState.value = "idle";
  animationState.value = "idle";
}

onMounted(refreshConnection);
onBeforeUnmount(() => {
  releasePreview();
  releaseCustomDrivingPreview();
});
</script>

<template>
  <main class="app-shell">
    <div class="ambient ambient-one" aria-hidden="true"></div>
    <div class="ambient ambient-two" aria-hidden="true"></div>

    <header class="topbar glass-surface">
      <a class="brand" href="#workspace" aria-label="Portrait Lab 首页">
        <span class="brand-mark" aria-hidden="true">
          <span class="brand-lens"><span class="brand-profile"></span></span>
          <span class="brand-spark">✦</span>
        </span>
        <span>Portrait Lab</span>
      </a>

      <div class="topbar-actions">
        <span class="service-status" :class="`is-${connectionState}`">
          <span class="status-dot" aria-hidden="true"></span>
          {{ connectionState === "online" ? "本地模型已连接" : connectionState === "checking" ? "正在连接" : "本地模型未连接" }}
        </span>
        <button class="icon-button" type="button" aria-label="重新检查服务" @click="refreshConnection">
          ↻
        </button>
      </div>
    </header>

    <section class="hero" aria-labelledby="hero-title">
      <p class="eyebrow">LOCAL · PRIVATE · DELIBERATE</p>
      <h1 id="hero-title">让珍贵照片，<br />回到清晰的记忆里。</h1>
      <p class="hero-copy">
        一个专注于照片修复的本地工作台。上传、检查、处理和下载都保持简单、可见、可控。
      </p>
      <div class="hero-pills" aria-label="服务能力">
        <span>本机私有运行</span><span>真实 GFPGAN</span><span>本地模型服务</span>
      </div>
    </section>

    <section id="workspace" class="workspace-grid" aria-label="照片修复工作台">
      <article class="glass-surface workflow-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">01 · 上传照片</p>
            <h2>从一张照片开始</h2>
          </div>
          <span class="mode-badge" :class="`is-${modelTone}`">{{ modelLabel }}</span>
        </div>

        <label
          class="dropzone"
          :class="{ 'is-dragging': dragActive, 'has-file': selectedFile }"
          @dragenter.prevent="dragActive = true"
          @dragover.prevent="dragActive = true"
          @dragleave.prevent="dragActive = false"
          @drop.prevent="onDrop"
        >
          <input type="file" accept="image/jpeg,image/png,image/webp" @change="onFileChange" />
          <span class="dropzone-orbit" aria-hidden="true">✦</span>
          <span class="dropzone-title">{{ selectedFile ? "替换这张照片" : "拖放一张照片到这里" }}</span>
          <span class="dropzone-copy">或从设备中选择 · JPG、PNG、WebP · 最大 15 MB</span>
          <span class="select-file">选择照片</span>
        </label>

        <div v-if="selectedFile" class="file-row">
          <div class="file-type" aria-hidden="true">IMG</div>
          <div class="file-copy">
            <strong>{{ selectedFile.name }}</strong>
            <span>{{ (selectedFile.size / 1024 / 1024).toFixed(2) }} MB · 已准备好</span>
          </div>
          <button class="text-button" type="button" @click="resetWorkspace">移除</button>
        </div>

        <div class="settings-block scale-settings" :class="`is-scale-${scale}`" :style="{ '--scale-progress': scaleProgress }">
          <div class="setting-label-row">
            <label for="scale">修复倍率</label>
            <output for="scale"><strong>{{ scale }}×</strong><span>{{ activeScaleLevel.label }}</span></output>
          </div>
          <div class="scale-range-wrap">
            <span class="scale-particle-field" aria-hidden="true"><i v-for="index in 24" :key="index" /></span>
            <input
              id="scale"
              v-model.number="scale"
              class="range-input"
              type="range"
              min="1"
              max="4"
              step="1"
              aria-describedby="scale-help"
            />
          </div>
          <div class="scale-levels" aria-hidden="true">
            <span v-for="level in scaleLevels" :key="level.value" :class="{ active: scale === level.value }" :style="{ '--level-position': level.position }">
              <strong>{{ level.value }}×</strong><small>{{ level.label }}</small>
            </span>
          </div>
          <p id="scale-help">倍率逐级提升，色彩与尺度同步增强；较高倍率会产生更大的输出文件。</p>
        </div>

        <button class="primary-button" type="button" :disabled="!canProcess || processingState === 'processing'" @click="processPhoto">
          <span>{{ processButtonLabel }}</span>
          <span aria-hidden="true">→</span>
        </button>
        <p v-if="restoreErrorMessage" class="inline-message is-error" role="alert">{{ restoreErrorMessage }}</p>
        <p v-else class="inline-message">{{ restoreStatusMessage }}</p>
      </article>

      <article class="glass-surface preview-panel">
        <div class="panel-heading">
          <div>
            <p class="section-kicker">02 · 检查结果</p>
            <h2>在提交前后，始终看得见</h2>
          </div>
          <span v-if="processingState === 'complete'" class="result-badge">已完成</span>
        </div>

        <div v-if="localPreviewUrl" class="comparison-grid">
          <figure class="image-frame">
            <img :src="sourceUrl || localPreviewUrl" alt="上传的原始照片预览" />
            <figcaption>原始照片</figcaption>
          </figure>
          <figure class="image-frame is-result">
            <img v-if="resultUrl" :src="resultUrl" alt="处理后的照片结果" />
            <div v-else class="result-placeholder">
              <span aria-hidden="true">✦</span>
              <p>{{ processingState === "processing" ? "正在处理图像…" : "处理结果将在这里显示" }}</p>
            </div>
            <figcaption>{{ resultUrl ? "处理结果" : "等待处理" }}</figcaption>
          </figure>
        </div>
        <div v-else class="empty-preview">
          <div class="preview-spark" aria-hidden="true">✦</div>
          <h3>结果会留在这里</h3>
          <p>上传照片后，可在同一视图中比较原图与处理结果。</p>
        </div>

        <div v-if="resultUrl" class="result-actions">
          <a class="secondary-button" :href="resultUrl" download>下载结果</a>
          <button class="text-button" type="button" @click="resetWorkspace">处理另一张</button>
        </div>
      </article>
    </section>

    <section class="motion-panel glass-surface" aria-labelledby="motion-title">
      <div class="motion-copy">
        <div class="motion-intro">
          <p class="section-kicker">03 · 动态驱动</p>
          <h2 id="motion-title">让照片自然动起来</h2>
          <p>
            选一段动作视频，模型会把其中的表情、口型和头部动作迁移到你的照片。悬停卡片即可预览；也可以上传你有权使用的视频。
          </p>
        </div>
        <label class="consent-check">
          <input v-model="consentConfirmed" type="checkbox" />
          <span>我确认拥有上传照片及驱动素材的使用授权。</span>
        </label>
      </div>
      <div class="motion-library" :class="{ 'is-muted': !consentConfirmed }">
        <div class="motion-library-heading">
          <div>
            <span class="motion-library-title">官方示例动作</span>
            <p>悬停预览，点击选择</p>
          </div>
          <span class="motion-library-count">{{ motionOptions.length }} 段</span>
        </div>
        <div v-if="motionOptions.length" class="motion-card-grid">
          <button
            v-for="motion in motionOptions"
            :key="motion.id"
            class="motion-card"
            :class="{ selected: selectedMotion === motion.id }"
            type="button"
            :aria-pressed="selectedMotion === motion.id"
            :aria-label="`选择驱动视频：${motion.label}，${motion.description}`"
            @click="selectSampleMotion(motion.id)"
            @mouseenter="playMotionPreview"
            @mouseleave="stopMotionPreview"
            @focus="playMotionPreview"
            @blur="stopMotionPreview"
          >
            <span class="motion-card-media">
              <video
                :src="motion.previewUrl"
                muted
                loop
                playsinline
                preload="metadata"
              >
                驱动视频预览不可用。
              </video>
              <span class="motion-card-play" aria-hidden="true">▶</span>
            </span>
            <span class="motion-card-copy">
              <strong>{{ motion.label }}</strong>
              <small>{{ motion.description }}</small>
              <em>{{ motion.duration }}</em>
            </span>
            <span v-if="selectedMotion === motion.id" class="motion-card-check" aria-hidden="true">✓</span>
          </button>
        </div>
        <p v-else class="motion-library-empty">连接本地服务后显示可用的本地驱动素材。</p>

        <div class="motion-upload-divider"><span>或使用自己的视频</span></div>
        <label class="custom-motion-picker" :class="{ selected: customDrivingVideo }">
          <input type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm" @change="onCustomDrivingVideoChange" />
          <span class="custom-motion-icon" aria-hidden="true">↑</span>
          <span class="custom-motion-copy">
            <strong>{{ customDrivingVideo ? customDrivingVideo.name : "上传自己的驱动视频" }}</strong>
            <small>
              {{ customDrivingVideo ? `${(customDrivingVideo.size / 1024 / 1024).toFixed(1)} MB · 仅用于本次生成` : "MP4、MOV、WebM · 最大 64 MB · 不会加入素材库" }}
            </small>
          </span>
          <span class="custom-motion-action">选择视频</span>
        </label>
        <div v-if="customDrivingPreviewUrl" class="custom-motion-preview">
          <video :src="customDrivingPreviewUrl" controls muted preload="metadata">你的浏览器不支持视频预览。</video>
          <button class="text-button" type="button" @click="clearCustomDrivingVideo">移除</button>
        </div>
      </div>
      <div class="motion-actions">
        <div class="motion-status">
          <span class="mode-badge" :class="`is-${animationTone}`">{{ animationLabel }}</span>
          <strong class="selected-driving-label">已选：{{ selectedDrivingLabel }}</strong>
          <p v-if="animationState === 'processing'">正在根据“{{ selectedDrivingLabel }}”生成短片；本机首次生成通常需要几分钟。</p>
          <p v-else-if="animationState === 'complete'">{{ animationStatusMessage }}</p>
          <p v-else-if="animationState === 'error'" class="is-error">{{ animationErrorMessage }}</p>
          <p v-else>上传同一张照片、选择动作并确认授权后，即可生成短片。</p>
        </div>
        <button
          class="primary-button motion-button"
          type="button"
          :disabled="!canAnimate || animationState === 'processing'"
          @click="generateAnimation"
        >
          <span>{{ animationButtonLabel }}</span><span aria-hidden="true">→</span>
        </button>
      </div>
      <div v-if="animationResultUrl" class="animation-result">
        <video :src="animationResultUrl" controls preload="metadata">你的浏览器不支持视频预览。</video>
        <a class="secondary-button" :href="animationResultUrl" download>下载动态短片</a>
      </div>
    </section>

    <footer class="footer-note">
      <span>图片只会发送到你选择的本机模型服务。</span>
      <span>GFPGAN 与 LivePortrait 都必须完成真实本机推理后，界面才会显示已就绪。</span>
    </footer>
  </main>
</template>
