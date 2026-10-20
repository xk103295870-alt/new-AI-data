<script setup lang="ts">
// 系统设置对话框：LLM 参数 + 数据源连接信息/授权白名单。
// 打开时拉取快照回填表单；api_key/password 永不回填真实值，仅以占位文案提示。
// “测试连接”只读验证不写库；“保存”写库成功后重拉快照；数据源保存成功 emit saved
// 由父组件刷新数据源状态。倒表（discover）发现表结构后勾选导入为授权白名单。
// 所有展示文本走 i18n；服务端固定 error code 本地化，不回显自由文本。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  SettingsRequestError,
  discoverDatasourceTables,
  fetchSettings,
  testDatasourceSettings,
  testLlmSettings,
  updateDatasourceSettings,
  updateLlmSettings,
} from '@/api/settings'
import type {
  DatasourceTestPayload,
  DiscoveredObject,
  LlmSettingsPayload,
  SettingsSnapshot,
} from '@/types/settings'
import {
  dsTestErrorText,
  llmTestErrorText,
  settingsErrorText,
  translate,
} from '@/i18n'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>()

const dialogVisible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const activeTab = ref<'llm' | 'datasource'>('llm')

const snapshot = ref<SettingsSnapshot | null>(null)
const loading = ref(false)

const llmForm = reactive({
  base_url: '',
  api_key: '',
  model: '',
  timeout_seconds: 90,
  max_tokens: 1024,
  temperature: 0,
})

const dsForm = reactive({
  enabled: false,
  name: '',
  host: '',
  port: 3306,
  database: '',
  user: '',
  password: '',
  ssl_enabled: false,
})

/** 授权白名单（对象名 → 列名列表），保存时随数据源表单一并提交。 */
const allowedObjects = ref<Record<string, string[]>>({})

const savingLlm = ref(false)
const testingLlm = ref(false)
const savingDs = ref(false)
const testingDs = ref(false)
const loadingTables = ref(false)

const discovered = ref<DiscoveredObject[]>([])
const selectedTables = ref<string[]>([])
const discoverFailed = ref(false)

const apiKeyPlaceholder = computed(() =>
  snapshot.value?.llm.api_key_set ? translate('settings.secretSaved') : '',
)
const passwordPlaceholder = computed(() =>
  snapshot.value?.datasource.password_set ? translate('settings.secretSaved') : '',
)

const allowedSummary = computed(() =>
  Object.entries(allowedObjects.value).map(([name, cols]) =>
    translate('settings.allowedItem', {
      name,
      cols: translate('settings.columns', { n: cols.length }),
    }),
  ),
)

function applySnapshot(next: SettingsSnapshot): void {
  snapshot.value = next
  llmForm.base_url = next.llm.base_url
  llmForm.api_key = ''
  llmForm.model = next.llm.model
  llmForm.timeout_seconds = next.llm.timeout_seconds
  llmForm.max_tokens = next.llm.max_tokens
  llmForm.temperature = next.llm.temperature

  dsForm.enabled = next.datasource.enabled
  dsForm.name = next.datasource.name
  dsForm.host = next.datasource.host
  dsForm.port = next.datasource.port
  dsForm.database = next.datasource.database
  dsForm.user = next.datasource.user
  dsForm.password = ''
  dsForm.ssl_enabled = next.datasource.ssl_enabled
  allowedObjects.value = { ...next.datasource.allowed_objects }
}

async function loadSettings(): Promise<void> {
  loading.value = true
  try {
    const resp = await fetchSettings()
    applySnapshot(resp.data)
  } catch (err) {
    if (err instanceof SettingsRequestError) {
      ElMessage.error(settingsErrorText(err.kind))
    } else {
      ElMessage.error(settingsErrorText('http'))
    }
  } finally {
    loading.value = false
  }
}

// 每次打开对话框都重新拉取快照，保证展示的是服务端最新配置。
watch(
  () => props.modelValue,
  (visible) => {
    if (visible) {
      discovered.value = []
      selectedTables.value = []
      discoverFailed.value = false
      void loadSettings()
    }
  },
)

/** 收集当前 LLM 表单值；密钥/密码留空则不发送（服务端保留原值）。
 * 保存与测试共用：测试端点全字段可选，必填结构向其赋值兼容。 */
function collectLlmPayload(): LlmSettingsPayload {
  const payload: LlmSettingsPayload = {
    base_url: llmForm.base_url,
    model: llmForm.model,
    timeout_seconds: llmForm.timeout_seconds,
    max_tokens: llmForm.max_tokens,
    temperature: llmForm.temperature,
  }
  if (llmForm.api_key.trim() !== '') payload.api_key = llmForm.api_key
  return payload
}

async function handleTestLlm(): Promise<void> {
  if (testingLlm.value) return
  testingLlm.value = true
  try {
    const resp = await testLlmSettings(collectLlmPayload())
    if (resp.data.ok) {
      ElMessage.success(
        translate('settings.testSuccess', { ms: resp.data.latency_ms ?? '—' }),
      )
    } else {
      ElMessage.warning(
        translate('settings.testFailed', { reason: llmTestErrorText(resp.data.error) }),
      )
    }
  } catch (err) {
    ElMessage.warning(
      translate('settings.testFailed', {
        reason: err instanceof SettingsRequestError ? settingsErrorText(err.kind) : settingsErrorText('http'),
      }),
    )
  } finally {
    testingLlm.value = false
  }
}

async function handleSaveLlm(): Promise<void> {
  if (savingLlm.value) return
  savingLlm.value = true
  try {
    const resp = await updateLlmSettings(collectLlmPayload())
    applySnapshot(resp.data)
    ElMessage.success(translate('settings.saved'))
  } catch (err) {
    ElMessage.error(err instanceof SettingsRequestError ? settingsErrorText(err.kind) : settingsErrorText('http'))
  } finally {
    savingLlm.value = false
  }
}

/** 收集数据源连接字段；密码留空则不发送（服务端保留原值）。 */
function collectDsTestPayload(): DatasourceTestPayload {
  const payload: DatasourceTestPayload = {
    host: dsForm.host,
    port: dsForm.port,
    database: dsForm.database,
    user: dsForm.user,
    ssl_enabled: dsForm.ssl_enabled,
  }
  if (dsForm.password.trim() !== '') payload.password = dsForm.password
  return payload
}

async function handleTestDatasource(): Promise<void> {
  if (testingDs.value) return
  testingDs.value = true
  try {
    const resp = await testDatasourceSettings(collectDsTestPayload())
    if (resp.data.ok) {
      ElMessage.success(
        translate('settings.testSuccess', { ms: resp.data.latency_ms ?? '—' }),
      )
    } else {
      ElMessage.warning(
        translate('settings.testFailed', { reason: dsTestErrorText(resp.data.error) }),
      )
    }
  } catch (err) {
    ElMessage.warning(
      translate('settings.testFailed', {
        reason: err instanceof SettingsRequestError ? settingsErrorText(err.kind) : settingsErrorText('http'),
      }),
    )
  } finally {
    testingDs.value = false
  }
}

async function handleSaveDatasource(): Promise<void> {
  if (savingDs.value) return
  savingDs.value = true
  try {
    const resp = await updateDatasourceSettings({
      enabled: dsForm.enabled,
      name: dsForm.name,
      host: dsForm.host,
      port: dsForm.port,
      database: dsForm.database,
      user: dsForm.user,
      ssl_enabled: dsForm.ssl_enabled,
      allowed_objects: { ...allowedObjects.value },
      ...(dsForm.password.trim() !== '' ? { password: dsForm.password } : {}),
    })
    applySnapshot(resp.data)
    ElMessage.success(translate('settings.saved'))
    emit('saved')
  } catch (err) {
    ElMessage.error(err instanceof SettingsRequestError ? settingsErrorText(err.kind) : settingsErrorText('http'))
  } finally {
    savingDs.value = false
  }
}

async function handleLoadTables(): Promise<void> {
  if (loadingTables.value) return
  loadingTables.value = true
  discoverFailed.value = false
  try {
    const resp = await discoverDatasourceTables()
    discovered.value = resp.data.objects
    selectedTables.value = []
  } catch (err) {
    discovered.value = []
    selectedTables.value = []
    discoverFailed.value = true
    ElMessage.error(err instanceof SettingsRequestError ? settingsErrorText(err.kind) : settingsErrorText('http'))
  } finally {
    loadingTables.value = false
  }
}

function selectAllTables(): void {
  selectedTables.value = discovered.value.map((obj) => obj.name)
}

function clearTables(): void {
  selectedTables.value = []
}

/** 勾选导入：把选中的表构建为授权白名单（表名 → 该表全部列名）写回表单状态。 */
function handleImportTables(): void {
  if (selectedTables.value.length === 0) {
    ElMessage.warning(translate('settings.noTablesSelected'))
    return
  }
  const next: Record<string, string[]> = {}
  for (const obj of discovered.value) {
    if (selectedTables.value.includes(obj.name)) {
      next[obj.name] = obj.columns.map((col) => col.name)
    }
  }
  allowedObjects.value = next
}

/** 复选框标签：表名（N 列），附 table/view 标注。 */
function tableLabel(obj: DiscoveredObject): string {
  return `${obj.name}（${translate('settings.columns', { n: obj.columns.length })}）`
}
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    :title="translate('settings.title')"
    width="640px"
    top="8vh"
  >
    <el-tabs v-model="activeTab">
      <!-- LLM 设置 -->
      <el-tab-pane :label="translate('settings.tabLlm')" name="llm">
        <el-form label-width="140px" :disabled="loading">
          <el-form-item :label="translate('settings.field.baseUrl')">
            <el-input v-model="llmForm.base_url" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.apiKey')">
            <el-input
              v-model="llmForm.api_key"
              type="password"
              show-password
              :placeholder="apiKeyPlaceholder"
            />
          </el-form-item>
          <el-form-item :label="translate('settings.field.model')">
            <el-input v-model="llmForm.model" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.timeout')">
            <el-input-number v-model="llmForm.timeout_seconds" :min="10" :max="120" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.maxTokens')">
            <el-input-number v-model="llmForm.max_tokens" :min="1" :max="8192" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.temperature')">
            <el-slider
              v-model="llmForm.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              show-input
            />
          </el-form-item>
          <el-form-item>
            <el-button
              :loading="testingLlm"
              :disabled="savingLlm || loading"
              @click="handleTestLlm"
            >
              {{ testingLlm ? translate('settings.testing') : translate('settings.test') }}
            </el-button>
            <el-button
              type="primary"
              :loading="savingLlm"
              :disabled="testingLlm || loading"
              @click="handleSaveLlm"
            >
              {{ savingLlm ? translate('settings.saving') : translate('settings.save') }}
            </el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- 数据源设置 -->
      <el-tab-pane :label="translate('settings.tabDatasource')" name="datasource">
        <el-form label-width="140px" :disabled="loading">
          <el-form-item :label="translate('settings.field.enabled')">
            <el-switch v-model="dsForm.enabled" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.name')">
            <el-input v-model="dsForm.name" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.host')">
            <el-input v-model="dsForm.host" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.port')">
            <el-input-number v-model="dsForm.port" :min="1" :max="65535" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.database')">
            <el-input v-model="dsForm.database" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.user')">
            <el-input v-model="dsForm.user" />
          </el-form-item>
          <el-form-item :label="translate('settings.field.password')">
            <el-input
              v-model="dsForm.password"
              type="password"
              show-password
              :placeholder="passwordPlaceholder"
            />
          </el-form-item>
          <el-form-item :label="translate('settings.field.ssl')">
            <el-switch v-model="dsForm.ssl_enabled" />
          </el-form-item>
          <el-form-item>
            <el-button
              :loading="testingDs"
              :disabled="savingDs || loading"
              @click="handleTestDatasource"
            >
              {{ testingDs ? translate('settings.testing') : translate('settings.test') }}
            </el-button>
            <el-button
              type="primary"
              :loading="savingDs"
              :disabled="testingDs || loading"
              @click="handleSaveDatasource"
            >
              {{ savingDs ? translate('settings.saving') : translate('settings.save') }}
            </el-button>
          </el-form-item>
        </el-form>

        <!-- 倒表（导入授权表）区域 -->
        <div class="discover-section">
          <div class="discover-title">{{ translate('settings.discoverTitle') }}</div>
          <div class="discover-actions">
            <el-button
              size="small"
              :loading="loadingTables"
              :disabled="loading || savingDs"
              @click="handleLoadTables"
            >
              {{
                loadingTables ? translate('settings.loadingTables') : translate('settings.loadTables')
              }}
            </el-button>
            <template v-if="discovered.length">
              <el-button size="small" text type="primary" @click="selectAllTables">
                {{ translate('settings.selectAll') }}
              </el-button>
              <el-button size="small" text type="primary" @click="clearTables">
                {{ translate('settings.clearAll') }}
              </el-button>
            </template>
          </div>
          <div v-if="discoverFailed" class="discover-hint">
            {{ translate('settings.loadTablesFailed') }}
          </div>
          <div v-else-if="!loadingTables && discovered.length === 0" class="discover-hint">
            {{ translate('settings.tablesEmpty') }}
          </div>
          <el-checkbox-group v-else v-model="selectedTables" class="discover-list">
            <el-checkbox v-for="obj in discovered" :key="obj.name" :value="obj.name">
              {{ tableLabel(obj) }}
              <span class="kind-tag">
                {{ translate(obj.kind === 'view' ? 'datasource.view' : 'datasource.table') }}
              </span>
            </el-checkbox>
          </el-checkbox-group>
          <div class="discover-actions">
            <el-button
              size="small"
              type="primary"
              :disabled="selectedTables.length === 0 || savingDs"
              @click="handleImportTables"
            >
              {{ translate('settings.importSelected') }}
            </el-button>
          </div>
        </div>

        <!-- 当前已授权对象摘要 -->
        <div v-if="allowedSummary.length" class="allowed-summary">
          <div class="discover-title">
            {{ translate('settings.allowedSummary', { count: allowedSummary.length }) }}
          </div>
          <ul>
            <li v-for="item in allowedSummary" :key="item">{{ item }}</li>
          </ul>
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-dialog>
</template>

<style scoped>
.discover-section {
  margin-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 12px;
}
.discover-title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 8px;
}
.discover-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.discover-hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 8px;
}
.discover-list {
  max-height: 240px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  margin-bottom: 8px;
}
.kind-tag {
  margin-left: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.allowed-summary {
  margin-top: 12px;
}
.allowed-summary ul {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
</style>
