<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const snapshot = ref(null)
const escrow = ref([])
const quarantine = ref([])
const scan = ref(null)
const runs = ref([])
const activeRun = ref(null)
const msg = ref('')
const ok = ref(false)
const busy = ref(false)

async function loadAll() {
  ;[snapshot.value, escrow.value, quarantine.value, runs.value] = await Promise.all([
    getJson('/api/repatriate/snapshot'),
    getJson('/api/repatriate/escrow'),
    getJson('/api/repatriate/quarantine'),
    getJson('/api/repatriate/runs'),
  ])
  if (runs.value.length) activeRun.value = await getJson(`/api/repatriate/runs/${runs.value[0].id}`)
}

onMounted(async () => {
  try {
    await loadAll()
  } catch (e) {
    msg.value = String(e.message || e)
  }
})

async function doScan() {
  msg.value = ''
  ok.value = false
  try {
    scan.value = await getJson('/api/repatriate/scan')
    msg.value = `预扫描：扫描 ${scan.value.scanned_count} 批；可归位 ${scan.value.repatriate_candidates}，可隔离 ${scan.value.quarantine_candidates}，保留 ${scan.value.keep_candidates}`
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function doRun() {
  msg.value = ''
  ok.value = false
  busy.value = true
  try {
    activeRun.value = await postJson('/api/repatriate/run')
    ok.value = activeRun.value.status === '已完成'
    msg.value = activeRun.value.message || ''
    await loadAll()
  } catch (e) {
    msg.value = `归位任务失败：${String(e.message || e)}`
    await loadAll()
  } finally {
    busy.value = false
  }
}

function runLabel(r) {
  return `#${r.id} · ${r.status} · 归位 ${r.repatriated_count} / 隔离 ${r.quarantined_count}`
}
</script>

<template>
  <div>
    <h1>托管归位与孤儿隔离</h1>
    <p class="lead">
      扫描托管态批次：挂靠交班单已失败/取消/不存在则整批回交班方在途并解除托管；进行中的单不得擅自归位。
      无主键（不在任何班次在途、不在托管、未结清）进入孤儿隔离区并留原因。
    </p>
    <p v-if="msg" class="msg" :class="{ ok }">{{ msg }}</p>

    <div class="panel">
      <div class="row">
        <button class="btn ghost" :disabled="busy" @click="doScan">预扫描（只读）</button>
        <button class="btn" :disabled="busy" @click="doRun">执行归位任务</button>
      </div>
      <div class="stats" v-if="snapshot" style="margin-bottom:0">
        <div class="stat"><div class="k">托管集合</div><div class="v">{{ snapshot.escrow_count }}</div></div>
        <div class="stat"><div class="k">在途集合</div><div class="v">{{ snapshot.inflight_count }}</div></div>
        <div class="stat"><div class="k">孤儿隔离区</div><div class="v">{{ snapshot.quarantine_count }}</div></div>
        <div class="stat"><div class="k">已结清</div><div class="v">{{ snapshot.settled_count }}</div></div>
      </div>
    </div>

    <div class="panel" v-if="scan">
      <h2>预扫描明细（未写入）</h2>
      <table>
        <thead><tr><th>业务键</th><th>标题</th><th>现行状态</th><th>归位去向</th><th>判定</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="i in scan.items" :key="i.batch_id">
            <td>{{ i.biz_key }}</td>
            <td>{{ i.title }}</td>
            <td>{{ i.prev_status }}</td>
            <td>{{ i.action === '归位' ? `班次#${i.target_shift_id}` : '—' }}</td>
            <td>
              <span class="tag" :class="{ on: i.action === '归位' }">{{ i.action }}</span>
            </td>
            <td>{{ i.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="activeRun">
      <h2>归位任务报告 #{{ activeRun.id }} · {{ activeRun.status }}</h2>
      <p class="lead" style="margin-bottom:12px">{{ activeRun.message }}</p>
      <div class="stats">
        <div class="stat"><div class="k">扫描数</div><div class="v">{{ activeRun.scanned_count }}</div></div>
        <div class="stat"><div class="k">归位数</div><div class="v">{{ activeRun.repatriated_count }}</div></div>
        <div class="stat"><div class="k">孤儿隔离数</div><div class="v">{{ activeRun.quarantined_count }}</div></div>
        <div class="stat"><div class="k">保留（进行中交班）</div><div class="v">{{ activeRun.kept_count }}</div></div>
      </div>
      <table v-if="activeRun.items && activeRun.items.length">
        <thead><tr><th>业务键</th><th>标题</th><th>动作</th><th>去向</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="i in activeRun.items" :key="i.batch_id">
            <td>{{ i.biz_key }}</td>
            <td>{{ i.title || '—' }}</td>
            <td><span class="tag" :class="{ on: i.action === '归位' }">{{ i.action }}</span></td>
            <td>{{ i.owner_shift_name || '—' }}</td>
            <td>{{ i.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>托管中批次（{{ escrow.length }}）</h2>
      <p class="lead" style="margin-bottom:12px">进行中交班单挂靠的批次会保留在托管集合，不会被归位任务清理回在途。</p>
      <table v-if="escrow.length">
        <thead><tr><th>业务键</th><th>标题</th><th>归属班次</th><th>挂靠交班单</th><th>单据状态</th></tr></thead>
        <tbody>
          <tr v-for="b in escrow" :key="b.id">
            <td>{{ b.biz_key }}</td>
            <td>{{ b.title }}</td>
            <td>{{ b.owner_shift_name || '—' }}</td>
            <td>#{{ b.escrow_handover_id }}</td>
            <td>{{ b.handover_status || '交班单不存在' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="lead" style="margin:0">当前没有托管中的批次。</p>
    </div>

    <div class="panel">
      <h2>孤儿隔离区（{{ quarantine.length }}）</h2>
      <p class="lead" style="margin-bottom:12px">无主键不删除、也不塞给任何班次；隔离原因与来源归位任务一并留痕。</p>
      <table v-if="quarantine.length">
        <thead><tr><th>业务键</th><th>标题</th><th>原状态</th><th>隔离原因</th><th>隔离时间</th><th>来源任务</th></tr></thead>
        <tbody>
          <tr v-for="b in quarantine" :key="b.id">
            <td>{{ b.biz_key }}</td>
            <td>{{ b.title }}</td>
            <td>{{ b.orphan_prev_status || '—' }}</td>
            <td>{{ b.orphan_reason }}</td>
            <td>{{ b.orphaned_at || '—' }}</td>
            <td>{{ b.orphan_run_id ? `#${b.orphan_run_id}` : '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="lead" style="margin:0">隔离区为空。</p>
    </div>

    <div class="panel">
      <h2>归位任务历史</h2>
      <table v-if="runs.length">
        <thead><tr><th>任务</th><th>状态</th><th>扫描</th><th>归位</th><th>孤儿隔离</th><th>保留</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>#{{ r.id }}</td>
            <td><span class="tag" :class="{ on: r.status === '已完成' }">{{ r.status }}</span></td>
            <td>{{ r.scanned_count }}</td>
            <td>{{ r.repatriated_count }}</td>
            <td>{{ r.quarantined_count }}</td>
            <td>{{ r.kept_count }}</td>
            <td>{{ r.finished_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="lead" style="margin:0">尚未执行过归位任务。</p>
    </div>
  </div>
</template>
