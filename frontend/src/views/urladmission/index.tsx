import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/urladmission'
const RULE_ENDPOINT = '/api/urlfilter/rule-versions'

type Row = Record<string, string | number | null>
type Hit = { stage: string; target: string; matched: boolean; pattern?: string; 规则版本: string; 重复记录ID?: number }
type Batch = Record<string, string | number | null> & { 规则快照?: Record<string, unknown> }

type TabKey = 'admit' | 'batches' | 'consumer' | 'rules'

const tabs: { key: TabKey; label: string }[] = [
  { key: 'admit', label: '准入处理' },
  { key: 'batches', label: '准入批次与审计明细' },
  { key: 'consumer', label: '抓取消费侧清单' },
  { key: 'rules', label: '规则版本快照' },
]

const decisionColor: Record<string, string> = {
  准入: '#1a7f37',
  拒绝: '#9a6700',
  校验失败: '#cf222e',
}

const crawlStatuses = ['抓取中', '抓取成功', '抓取失败', '已跳过']

async function postJson(path: string, body: unknown): Promise<{ ok: boolean; message: string; entry?: Record<string, unknown> | null }> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return response.json()
}

export default function UrlAdmissionView() {
  const [tab, setTab] = useState<TabKey>('admit')
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')

  return (
    <section className="page" data-module="urladmission">
      <header className="page-head">
        <div>
          <h2>URL 准入与去重清单</h2>
          <p className="page-desc">
            候选 URL 经规范化与规则评估后生成唯一一份去重清单；同一规范化 URL 只进清单一次，
            规则校验失败明确拒绝、绝不默认放行，每条记录保留规则版本与命中依据可供审计。
          </p>
        </div>
      </header>

      <nav className="nav-list" style={{ flexDirection: 'row', gap: 8, marginBottom: 16 }}>
        {tabs.map((item) => (
          <button
            key={item.key}
            className={`btn ${tab === item.key ? 'primary' : 'ghost'}`}
            type="button"
            onClick={() => { setTab(item.key); setErrorMessage(''); setNotice('') }}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {errorMessage && <p className="error-text">{errorMessage}</p>}
      {notice && <p style={{ color: '#1a7f37' }}>{notice}</p>}

      {tab === 'admit' && <AdmitPanel onDone={(msg) => { setNotice(msg); setTab('batches') }} onError={setErrorMessage} />}
      {tab === 'batches' && <BatchPanel onError={setErrorMessage} onNotice={setNotice} />}
      {tab === 'consumer' && <ConsumerPanel onError={setErrorMessage} onNotice={setNotice} />}
      {tab === 'rules' && <RuleVersionPanel onError={setErrorMessage} onNotice={setNotice} />}
    </section>
  )
}

// ---------------------------------------------------------------- 准入处理面板
function AdmitPanel({ onDone, onError }: { onDone: (msg: string) => void; onError: (msg: string) => void }) {
  const [candidateText, setCandidateText] = useState(
    [
      'https://example.com/News/Item-7?utm_source=mail&id=9&page=2',
      'HTTPS://EXAMPLE.COM/news/item-7/?page=2&id=9',
      'https://docs.example.com/guide/intro#chapter-1',
      'https://example.com/private/db-dump',
      'https://other.example.net/not-allowed',
      'http://[broken-host',
    ].join('\n'),
  )
  const [versions, setVersions] = useState<Row[]>([])
  const [version, setVersion] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const loadVersions = useCallback(async () => {
    try {
      const response = await request(`${RULE_ENDPOINT}?status=已生效&size=100`)
      if (!response.ok) throw new Error('生效规则版本读取失败')
      const payload = await response.json()
      setVersions(payload.items ?? [])
      if (!version && payload.items?.length) setVersion(String(payload.items[0].version))
    } catch (error) {
      onError(error instanceof Error ? error.message : '规则版本读取失败')
    }
  }, [onError, version])

  useEffect(() => { void loadVersions() }, [loadVersions])

  async function submit() {
    setSubmitting(true)
    try {
      const candidate_urls = candidateText.split('\n').map((line) => line.trim()).filter(Boolean)
      if (candidate_urls.length === 0) {
        onError('候选 URL 为空，请每行填写一个 URL')
        return
      }
      const result = await postJson(`${ENDPOINT}/batches`, {
        candidate_urls,
        规则版本: version || null,
      })
      if (!result.ok) {
        onError(result.message)
        return
      }
      onDone(result.message)
    } catch (error) {
      onError(error instanceof Error ? error.message : '准入处理提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div>
      <div className="filter-bar">
        <label className="filter-item">
          <span>生效规则版本（留空取最新）</span>
          <select value={version} onChange={(e) => setVersion(e.target.value)}>
            <option value="">最新生效版本</option>
            {versions.map((row) => (
              <option key={String(row.id)} value={String(row.version)}>
                {String(row.version)}（{String(row['规则编号'])}）
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="filter-item" style={{ display: 'block' }}>
        <span>候选 URL（每行一个；大小写、尾斜杠、参数顺序、跟踪参数差异会在规范化阶段折叠）</span>
        <textarea
          rows={10}
          style={{ width: '100%', fontFamily: 'monospace', marginTop: 8 }}
          value={candidateText}
          onChange={(e) => setCandidateText(e.target.value)}
        />
      </label>
      <div className="page-actions" style={{ marginTop: 12 }}>
        <button className="btn primary" type="button" disabled={submitting} onClick={() => void submit()}>
          {submitting ? '处理中…' : '生成准入结果与去重清单'}
        </button>
      </div>
      <p className="page-desc" style={{ marginTop: 12 }}>
        相同候选集与规则版本重复提交（含并发）只生效一次，服务端直接返回既有批次，不会产生第二份结果。
      </p>
    </div>
  )
}

// ---------------------------------------------------------------- 批次与审计面板
function BatchPanel({ onError, onNotice }: { onError: (msg: string) => void; onNotice: (msg: string) => void }) {
  const [batches, setBatches] = useState<Batch[]>([])
  const [selected, setSelected] = useState<Batch | null>(null)
  const [records, setRecords] = useState<Row[]>([])
  const [decisionFilter, setDecisionFilter] = useState('')

  const reload = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/batches?size=50`)
      if (!response.ok) throw new Error('准入批次读取失败')
      const payload = await response.json()
      setBatches(payload.items ?? [])
      if (!selected && payload.items?.length) void openBatch(payload.items[0] as Batch)
    } catch (error) {
      onError(error instanceof Error ? error.message : '准入批次读取失败')
    }
  }, [onError, selected])

  const openBatch = useCallback(async (batch: Batch) => {
    setSelected(batch)
    try {
      const query = decisionFilter ? `?decision=${encodeURIComponent(decisionFilter)}` : ''
      const response = await request(`${ENDPOINT}/batches/${batch.id}/records${query}`)
      if (!response.ok) throw new Error('准入明细读取失败')
      const payload = await response.json()
      setRecords(payload.items ?? [])
    } catch (error) {
      onError(error instanceof Error ? error.message : '准入明细读取失败')
    }
  }, [decisionFilter, onError])

  useEffect(() => { void reload() }, [reload])
  useEffect(() => { if (selected) void openBatch(selected) }, [decisionFilter])

  async function syncConsumer(batch: Batch) {
    try {
      const result = await postJson(`${ENDPOINT}/batches/${batch.id}/sync-consumer`, {})
      if (!result.ok) { onError(result.message); return }
      onNotice(result.message)
      void reload()
    } catch (error) {
      onError(error instanceof Error ? error.message : '消费侧同步失败')
    }
  }

  const snapshotText = useMemo(() => {
    if (!selected?.规则快照) return ''
    return JSON.stringify(selected.规则快照, null, 2)
  }, [selected])

  return (
    <div>
      <table className="data-table">
        <thead>
          <tr>
            <th>批次编号</th><th>规则版本</th><th>候选</th><th>准入</th><th>拒绝</th>
            <th>校验失败</th><th>重复剔除</th><th>消费侧同步</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {batches.map((batch) => (
            <tr key={String(batch.id)} style={selected?.id === batch.id ? { background: '#f0f7ff' } : undefined}>
              <td>{String(batch.batch_no)}</td>
              <td>{String(batch['规则版本'])}</td>
              <td>{String(batch['候选总数'])}</td>
              <td>{String(batch['准入数'])}</td>
              <td>{String(batch['拒绝数'])}</td>
              <td style={Number(batch['校验失败数']) > 0 ? { color: '#cf222e' } : undefined}>{String(batch['校验失败数'])}</td>
              <td>{String(batch['重复剔除数'])}</td>
              <td>{String(batch['消费侧同步'])}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openBatch(batch)}>审计明细</button>
                <button className="link" type="button" onClick={() => void syncConsumer(batch)}>同步消费侧</button>
              </td>
            </tr>
          ))}
          {batches.length === 0 && (
            <tr><td className="empty-state" colSpan={9}>暂无准入批次，请先在「准入处理」提交候选 URL</td></tr>
          )}
        </tbody>
      </table>

      {selected && (
        <div style={{ marginTop: 20 }}>
          <h3>批次 {String(selected.batch_no)} 的准入明细（去重清单版本：{String(selected['去重清单版本'])}）</h3>
          <div className="filter-bar">
            {['', '准入', '拒绝', '校验失败'].map((value) => (
              <button
                key={value || 'all'}
                className={`btn ${decisionFilter === value ? 'primary' : 'ghost'}`}
                type="button"
                onClick={() => setDecisionFilter(value)}
              >
                {value || '全部结论'}
              </button>
            ))}
            <button className="btn ghost" type="button" onClick={() => window.open(`${ENDPOINT}/batches/${selected.id}/export`, '_blank')}>
              导出审计明细
            </button>
          </div>
          <table className="data-table">
            <thead>
              <tr><th>ID</th><th>原始URL</th><th>规范化URL</th><th>结论</th><th>原因码</th><th>抓取状态</th><th>命中依据</th></tr>
            </thead>
            <tbody>
              {records.map((record) => {
                const hits = (record['命中依据'] as unknown as Hit[]) ?? []
                return (
                  <tr key={String(record.id)}>
                    <td>{String(record.id)}</td>
                    <td style={{ maxWidth: 260, wordBreak: 'break-all' }}>{String(record['原始URL'])}</td>
                    <td style={{ maxWidth: 260, wordBreak: 'break-all' }}>{record['规范化URL'] ? String(record['规范化URL']) : '—'}</td>
                    <td style={{ color: decisionColor[String(record['准入结论'])] }}>{String(record['准入结论'])}</td>
                    <td>{String(record['原因码'])}</td>
                    <td>{String(record['抓取状态'])}</td>
                    <td style={{ maxWidth: 320 }}>
                      {hits.map((hit, idx) => (
                        <div key={idx} style={{ fontSize: 12 }}>
                          {hit.stage}：{hit.matched ? '✅' : '❌'}
                          {hit.pattern ? `（${hit.pattern}）` : ''}
                        </div>
                      ))}
                    </td>
                  </tr>
                )
              })}
              {records.length === 0 && <tr><td className="empty-state" colSpan={7}>当前筛选下没有记录</td></tr>}
            </tbody>
          </table>
          <details style={{ marginTop: 12 }}>
            <summary className="link">查看冻结的规则快照（历史批次永久保留换版前口径）</summary>
            <pre style={{ background: '#f6f8fa', padding: 12, overflow: 'auto' }}>{snapshotText}</pre>
          </details>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 消费侧清单面板
function ConsumerPanel({ onError, onNotice }: { onError: (msg: string) => void; onNotice: (msg: string) => void }) {
  const [rows, setRows] = useState<Row[]>([])
  const [statusFilter, setStatusFilter] = useState('')

  const reload = useCallback(async () => {
    try {
      const query = statusFilter ? `?crawl_status=${encodeURIComponent(statusFilter)}` : ''
      const response = await request(`${ENDPOINT}/consumer-list${query}`)
      if (!response.ok) throw new Error('消费侧清单读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
    } catch (error) {
      onError(error instanceof Error ? error.message : '消费侧清单读取失败')
    }
  }, [onError, statusFilter])

  useEffect(() => { void reload() }, [reload])

  async function writeBack(row: Row, crawlStatus: string) {
    try {
      const result = await postJson(`${ENDPOINT}/consumer-list/write-back`, {
        canonical_key: String(row.canonical_key),
        抓取状态: crawlStatus,
        抓取详情: { source: '控制台模拟回写', at: new Date().toISOString() },
      })
      if (!result.ok) { onError(result.message); return }
      onNotice(result.message)
      void reload()
    } catch (error) {
      onError(error instanceof Error ? error.message : '抓取状态回写失败')
    }
  }

  return (
    <div>
      <p className="page-desc">
        该清单由准入结果驱动写入，重复同步幂等；抓取消费侧把实际状态写回后，对应准入记录同步更新。
      </p>
      <div className="filter-bar">
        {['', '待抓取', '抓取中', '抓取成功', '抓取失败', '已跳过'].map((value) => (
          <button
            key={value || 'all'}
            className={`btn ${statusFilter === value ? 'primary' : 'ghost'}`}
            type="button"
            onClick={() => setStatusFilter(value)}
          >
            {value || '全部状态'}
          </button>
        ))}
      </div>
      <table className="data-table">
        <thead>
          <tr><th>规范化URL</th><th>规则版本</th><th>来源批次</th><th>准入记录</th><th>抓取状态</th><th>最后回写</th><th>回写操作</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td style={{ maxWidth: 280, wordBreak: 'break-all' }}>{String(row['规范化URL'])}</td>
              <td>{String(row['规则版本'])}</td>
              <td>{((row['来源批次'] as unknown as string[]) ?? []).join('、')}</td>
              <td>#{String(row['准入记录ID'])}</td>
              <td>{String(row['抓取状态'])}</td>
              <td>{row['最后回写时间'] ? String(row['最后回写时间']) : '—'}</td>
              <td className="row-actions">
                {crawlStatuses.map((status) => (
                  <button key={status} className="link" type="button" onClick={() => void writeBack(row, status)}>
                    {status}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td className="empty-state" colSpan={7}>消费侧清单为空，准入 URL 后会自动写入</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------- 规则版本面板
function RuleVersionPanel({ onError, onNotice }: { onError: (msg: string) => void; onNotice: (msg: string) => void }) {
  const [rows, setRows] = useState<Row[]>([])
  const [form, setForm] = useState({
    规则编号: 'URLF-RULE-NEW',
    域名白名单: 'example.com, *.example.com',
    路径黑名单: '/private(/.*)?',
    包含模式: '^https?://',
    排除模式: '[?&]utm_block=1',
    参数剥离: 'utm_source, utm_medium, sessionid',
    大小写处理: 'lower',
    尾部斜杠策略: 'strip',
  })

  const reload = useCallback(async () => {
    try {
      const response = await request(`${RULE_ENDPOINT}?size=100`)
      if (!response.ok) throw new Error('规则版本读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
    } catch (error) {
      onError(error instanceof Error ? error.message : '规则版本读取失败')
    }
  }, [onError])

  useEffect(() => { void reload() }, [reload])

  function update(field: string, value: string) {
    setForm((prev) => ({ ...prev, [field]: value }))
  }

  async function publish() {
    try {
      const values = {
        ...form,
        域名白名单: form.域名白名单.split(',').map((s) => s.trim()).filter(Boolean),
        路径黑名单: form.路径黑名单.split(',').map((s) => s.trim()).filter(Boolean),
        包含模式: form.包含模式.split(',').map((s) => s.trim()).filter(Boolean),
        排除模式: form.排除模式.split(',').map((s) => s.trim()).filter(Boolean),
        参数剥离: form.参数剥离.split(',').map((s) => s.trim()).filter(Boolean),
        去除片段: true,
      }
      const result = await postJson(RULE_ENDPOINT, { values })
      if (!result.ok) { onError(result.message); return }
      onNotice(result.message)
      void reload()
    } catch (error) {
      onError(error instanceof Error ? error.message : '规则版本发布失败')
    }
  }

  async function deprecate(row: Row) {
    try {
      const response = await request(`${RULE_ENDPOINT}/${row.id}/deprecate`, { method: 'POST', body: JSON.stringify({}) })
      const result = await response.json()
      if (!result.ok) { onError(result.message); return }
      onNotice(result.message)
      void reload()
    } catch (error) {
      onError(error instanceof Error ? error.message : '规则版本废弃失败')
    }
  }

  return (
    <div>
      <h3>发布新规则版本（包含规则、排除条件、参数范围、判定标准均可配置）</h3>
      <div className="filter-bar" style={{ flexWrap: 'wrap' }}>
        {Object.entries(form).map(([field, value]) => (
          <label key={field} className="filter-item" style={{ minWidth: 240 }}>
            <span>{field}</span>
            {field === '大小写处理' ? (
              <select value={value} onChange={(e) => update(field, e.target.value)}>
                <option value="lower">lower（主机与路径小写）</option>
                <option value="preserve">preserve（保留路径大小写）</option>
              </select>
            ) : field === '尾部斜杠策略' ? (
              <select value={value} onChange={(e) => update(field, e.target.value)}>
                <option value="strip">strip（折叠尾斜杠）</option>
                <option value="keep">keep（保留尾斜杠）</option>
              </select>
            ) : (
              <input value={value} onChange={(e) => update(field, e.target.value)} />
            )}
          </label>
        ))}
        <button className="btn primary" type="button" onClick={() => void publish()}>发布版本快照</button>
      </div>
      <p className="page-desc">
        模式支持 Python 正则，或 <code>glob:</code> 前缀的通配符；发布时立即编译，非法模式得到明确失败结果、不会落库。
      </p>

      <table className="data-table" style={{ marginTop: 16 }}>
        <thead>
          <tr><th>版本号</th><th>规则编号</th><th>状态</th><th>发布时间</th><th>口径摘要</th><th>操作</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.version)}</td>
              <td>{String(row['规则编号'])}</td>
              <td>{String(row['状态'])}</td>
              <td>{String(row.published_at)}</td>
              <td style={{ maxWidth: 360, fontSize: 12 }}>
                白名单 {((row['域名白名单'] as unknown as string[]) ?? []).join('/')}；
                剥离 {((row['参数剥离'] as unknown as string[]) ?? []).join('/')}
              </td>
              <td className="row-actions">
                {row['状态'] === '已生效' && (
                  <button className="link" type="button" onClick={() => void deprecate(row)}>废弃版本</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td className="empty-state" colSpan={6}>暂无规则版本</td></tr>}
        </tbody>
      </table>
    </div>
  )
}
