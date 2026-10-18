<script setup lang="ts">
// 页面布局：顶部标题 + 后端服务状态；左侧导航（新建对话）；主区域为数据源状态与聊天面板。
// “新建对话”只清空前端消息与会话 ID；历史会话列表不在本阶段范围。
import { onMounted, ref } from 'vue'
import ServiceStatus from '@/components/ServiceStatus.vue'
import DataSourceStatus from '@/components/DataSourceStatus.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import { useServiceStatus } from '@/composables/useServiceStatus'
import { locale, setLocale, translate, type Locale } from '@/i18n'

const { state, message, check } = useServiceStatus()
onMounted(check)

const chatPanel = ref<InstanceType<typeof ChatPanel> | null>(null)

/** 新建对话：清空消息与会话 ID；在途请求期间由 ChatPanel 自行忽略。 */
function newConversation(): void {
  chatPanel.value?.reset()
}

/** 语言切换只更新展示层；不重载页面、不清空输入/会话、不触发任何请求。 */
function chooseLocale(next: Locale): void {
  setLocale(next)
}
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <h1 class="app-title">{{ translate('app.title') }}</h1>
      <ServiceStatus :state="state" :message="message" />
      <div class="locale-switcher" role="group" :aria-label="translate('locale.label')">
        <button
          type="button"
          class="locale-button"
          :class="{ active: locale === 'en' }"
          :aria-pressed="locale === 'en'"
          @click="chooseLocale('en')"
        >
          English
        </button>
        <button
          type="button"
          class="locale-button"
          :class="{ active: locale === 'zh' }"
          :aria-pressed="locale === 'zh'"
          @click="chooseLocale('zh')"
        >
          中文
        </button>
      </div>
    </el-header>
    <el-container class="app-body">
      <el-aside width="200px" class="app-aside">
        <el-button @click="newConversation">{{ translate('app.newConversation') }}</el-button>
        <!-- Skill 尚未实现，本阶段隐藏，不展示为可用。 -->
      </el-aside>
      <el-main class="app-main">
        <DataSourceStatus />
        <ChatPanel ref="chatPanel" />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
}
.app-header {
  display: flex;
  align-items: center;
  gap: 16px;
  height: auto;
  min-height: var(--el-header-height, 60px);
  flex-wrap: wrap;
  padding-top: 8px;
  padding-bottom: 8px;
}
.app-title {
  font-size: 18px;
  margin: 0;
}
.locale-switcher {
  display: inline-flex;
  margin-left: auto;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  overflow: hidden;
}
.locale-button {
  border: 0;
  background: transparent;
  color: var(--el-text-color-regular);
  cursor: pointer;
  padding: 4px 10px;
  font: inherit;
  line-height: 1.5;
}
.locale-button + .locale-button {
  border-left: 1px solid var(--el-border-color);
}
.locale-button.active {
  background: var(--el-color-primary);
  color: #fff;
}
.locale-button:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: -2px;
}
.app-aside {
  border-right: 1px solid var(--el-border-color);
  padding: 16px;
}
.app-main {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
@media (max-width: 720px) {
  .app-body {
    flex-direction: column;
  }
  .app-aside {
    width: auto !important;
    border-right: 0;
    border-bottom: 1px solid var(--el-border-color);
  }
  .locale-switcher {
    margin-left: 0;
  }
}
</style>
