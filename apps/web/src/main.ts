// 前端入口：创建 Vue 应用并全局注册 Element Plus。
// 本文件仅做装配，无业务逻辑（覆盖率豁免）。
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import { initI18n } from './i18n'

// 先同步 document.lang 与标题，再挂载应用，避免首屏语言属性与界面不一致。
initI18n()
createApp(App).use(ElementPlus).mount('#app')
