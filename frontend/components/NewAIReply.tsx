import React, { useEffect, useMemo, useState } from 'react';
import { Bot, Loader2, Play, Save, Sparkles } from 'lucide-react';
import { AccountDetail } from '../types';
import {
  getAccountDetails,
  getNewAIReplySettings,
  NewAIReplySettings,
  testNewAIReply,
  updateNewAIReplySettings,
} from '../services/api';
import { notify } from '../services/feedback';
import { EmptyState, PageHeader, PageLoading, SectionHeader } from './ui';

const emptySettings: NewAIReplySettings = {
  enabled: false,
  backend: 'codebuddy',
  codebuddy_bin: 'codebuddy',
  codebuddy_model: 'hy3',
  codebuddy_timeout: 90,
  codebuddy_available: false,
  api_key_configured: false,
  base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  model_name: 'qwen-plus',
  human_wait_seconds: 180,
  manual_mode_timeout: 3600,
  toggle_keywords: '。',
  prompt: '',
};

function errorText(error: unknown, fallback: string) {
  if (error && typeof error === 'object' && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
  }
  return error instanceof Error && error.message ? error.message : fallback;
}

const NewAIReply: React.FC = () => {
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState('');
  const [settings, setSettings] = useState<NewAIReplySettings>(emptySettings);
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testMessage, setTestMessage] = useState('这个电脑版能用吗？');
  const [testTitle, setTestTitle] = useState('测试商品');
  const [testPrice, setTestPrice] = useState('');
  const [testDesc, setTestDesc] = useState('仅用于测试，不会发送到闲鱼。');
  const [testReply, setTestReply] = useState('');

  const selectedAccount = useMemo(
    () => accounts.find(account => account.id === selectedAccountId),
    [accounts, selectedAccountId],
  );

  useEffect(() => {
    getAccountDetails()
      .then(data => {
        setAccounts(data);
        setSelectedAccountId(data[0]?.id || '');
      })
      .catch(error => notify(errorText(error, '账号加载失败'), 'error'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedAccountId) return;
    setLoading(true);
    setApiKey('');
    setTestReply('');
    getNewAIReplySettings(selectedAccountId)
      .then(data => setSettings({ ...emptySettings, ...data }))
      .catch(error => notify(errorText(error, '新AI配置加载失败'), 'error'))
      .finally(() => setLoading(false));
  }, [selectedAccountId]);

  const update = <K extends keyof NewAIReplySettings>(key: K, value: NewAIReplySettings[K]) => {
    setSettings(current => ({ ...current, [key]: value }));
  };

  const handleSave = async () => {
    if (!selectedAccountId) {
      notify('请先选择账号', 'warning');
      return;
    }
    setSaving(true);
    try {
      await updateNewAIReplySettings({ ...settings, cookie_id: selectedAccountId, api_key: apiKey });
      const refreshed = await getNewAIReplySettings(selectedAccountId);
      setSettings({ ...emptySettings, ...refreshed });
      setApiKey('');
      notify('新AI自动回复配置已保存', 'success');
    } catch (error) {
      notify(errorText(error, '配置保存失败'), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!selectedAccountId || !testMessage.trim()) {
      notify('请选择账号并输入测试消息', 'warning');
      return;
    }
    setTesting(true);
    setTestReply('');
    try {
      const result = await testNewAIReply({
        cookie_id: selectedAccountId,
        message: testMessage.trim(),
        item_title: testTitle.trim() || '测试商品',
        item_price: testPrice.trim(),
        item_desc: testDesc.trim() || '仅用于测试，不会发送到闲鱼。',
      });
      if (!result.would_send) {
        setTestReply('模型判断这条不发送。');
      } else {
        setTestReply(result.reply || '测试完成');
      }
      notify(result.intent ? `测试完成，路线 ${result.intent}` : '测试完成，未发送到闲鱼', 'success');
    } catch (error) {
      notify(errorText(error, '测试失败'), 'error');
    } finally {
      setTesting(false);
    }
  };

  if (loading && accounts.length === 0) {
    return <PageLoading label="正在加载新AI自动回复" />;
  }

  return (
    <div className="page-stack animate-fade-in">
      <PageHeader
        title="新AI自动回复"
        description="按账号配置模型、提示词和人工等待。"
        icon={Sparkles}
        actions={(
          <div className="flex min-w-0 flex-wrap items-end gap-2">
            <label className="min-w-0 sm:w-72">
              <span className="field-label">当前账号</span>
              <select
                value={selectedAccountId}
                onChange={event => setSelectedAccountId(event.target.value)}
                className="ios-input w-full rounded-md px-3 py-2 text-sm font-semibold"
              >
                {accounts.map(account => (
                  <option key={account.id} value={account.id}>
                    {account.nickname || account.remark || `账号 ${account.id.slice(0, 8)}`}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={handleSave}
              disabled={saving || !selectedAccountId}
              className="ios-btn-primary flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {saving ? '保存中' : '保存配置'}
            </button>
          </div>
        )}
      />

      {!selectedAccountId ? (
        <EmptyState icon={Bot} title="暂无可配置账号" description="请先在账号管理中添加并登录闲鱼账号。" />
      ) : (
        <>
          <section className="section-panel grid gap-4 p-4 lg:grid-cols-[1fr_auto] lg:items-center">
            <div>
              <div className="font-bold text-gray-900">
                {selectedAccount?.nickname || selectedAccount?.remark || selectedAccountId}
              </div>
            </div>
            <label className="flex cursor-pointer items-center gap-3">
              <span className="text-sm font-bold text-gray-700">{settings.enabled ? '已启用' : '已停用'}</span>
              <input
                type="checkbox"
                checked={settings.enabled}
                onChange={event => update('enabled', event.target.checked)}
                className="h-5 w-5 accent-yellow-400"
              />
            </label>
          </section>

          <div className="grid gap-6 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
            <div className="space-y-6">
              <section className="section-panel">
                <SectionHeader title="模型" description="当前使用的是 CodeBuddy。切换到兼容接口时才需要密钥。" icon={Bot} />
                <div className="grid gap-4 p-5 md:grid-cols-2">
                  <label>
                    <span className="field-label">后端</span>
                    <select
                      value={settings.backend}
                      onChange={event => update('backend', event.target.value as NewAIReplySettings['backend'])}
                      className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    >
                      <option value="codebuddy">CodeBuddy</option>
                      <option value="openai">OpenAI 兼容接口</option>
                    </select>
                  </label>
                  <label>
                    <span className="field-label">CodeBuddy 模型</span>
                    <input
                      value={settings.codebuddy_model}
                      onChange={event => update('codebuddy_model', event.target.value)}
                      className="ios-input w-full rounded-md px-3 py-2 text-sm"
                      placeholder="hy3"
                    />
                  </label>
                  <label>
                    <span className="field-label">超时（秒）</span>
                    <input
                      type="number"
                      min={5}
                      max={180}
                      value={settings.codebuddy_timeout}
                      onChange={event => update('codebuddy_timeout', Number(event.target.value))}
                      className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    />
                  </label>
                  <label>
                    <span className="field-label">命令</span>
                    <input
                      value={settings.codebuddy_bin}
                      onChange={event => update('codebuddy_bin', event.target.value)}
                      className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    />
                  </label>
                  <p className="md:col-span-2 text-xs text-gray-500">
                    {settings.codebuddy_available ? '本机已找到 CodeBuddy 命令。' : '本机还没找到这个命令，启用前请先确认已安装并登录。'}
                  </p>
                  {settings.backend === 'openai' && (
                    <>
                      <label className="md:col-span-2">
                        <span className="field-label">接口地址</span>
                        <input
                          value={settings.base_url}
                          onChange={event => update('base_url', event.target.value)}
                          className="ios-input w-full rounded-md px-3 py-2 text-sm"
                        />
                      </label>
                      <label>
                        <span className="field-label">模型名称</span>
                        <input
                          value={settings.model_name}
                          onChange={event => update('model_name', event.target.value)}
                          className="ios-input w-full rounded-md px-3 py-2 text-sm"
                        />
                      </label>
                      <label>
                        <span className="field-label">密钥</span>
                        <input
                          type="password"
                          value={apiKey}
                          onChange={event => setApiKey(event.target.value)}
                          className="ios-input w-full rounded-md px-3 py-2 text-sm"
                          placeholder={settings.api_key_configured ? '已配置，留空则保留' : '填写后保存'}
                          autoComplete="off"
                        />
                      </label>
                    </>
                  )}
                </div>
              </section>

              <section className="section-panel">
                <SectionHeader
                  title="统一提示词"
                  description="必须保留 CLASSIFY、DEFAULT、PRICE、TECH、VOICE 五个分区。"
                  icon={Sparkles}
                />
                <div className="p-5">
                  <textarea
                    value={settings.prompt}
                    onChange={event => update('prompt', event.target.value)}
                    className="ios-input min-h-[420px] w-full rounded-md px-3 py-3 font-mono text-xs leading-5"
                    spellCheck={false}
                  />
                </div>
              </section>
            </div>

            <div className="space-y-6">
              <section className="section-panel">
                <SectionHeader title="人工优先" description="卖家手动回复后，等待这段时间再自动回复。" icon={Bot} />
                <div className="space-y-4 p-5">
                  <label className="block">
                    <span className="field-label">等待秒数</span>
                    <input
                      type="number"
                      min={1}
                      value={settings.human_wait_seconds}
                      onChange={event => update('human_wait_seconds', Number(event.target.value))}
                      className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    />
                  </label>
                  <label className="block">
                    <span className="field-label">接管超时（秒）</span>
                    <input
                      type="number"
                      min={60}
                      value={settings.manual_mode_timeout}
                      onChange={event => update('manual_mode_timeout', Number(event.target.value))}
                      className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    />
                  </label>
                  <label className="block">
                    <span className="field-label">接管词</span>
                    <input
                      value={settings.toggle_keywords}
                      onChange={event => update('toggle_keywords', event.target.value)}
                      className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    />
                    <span className="mt-1 block text-xs text-gray-500">卖家发出的原文正好是这段文字时，切换该会话的人工接管。</span>
                  </label>
                </div>
              </section>

              <section className="section-panel">
                <SectionHeader title="试一句" description="使用已保存的配置，只在本机生成，不会发给买家。改完提示词要先保存。" icon={Play} />
                <div className="space-y-3 p-5">
                  <textarea
                    value={testMessage}
                    onChange={event => setTestMessage(event.target.value)}
                    className="ios-input min-h-20 w-full rounded-md px-3 py-2 text-sm"
                  />
                  <input
                    value={testTitle}
                    onChange={event => setTestTitle(event.target.value)}
                    className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    placeholder="商品标题"
                  />
                  <input
                    value={testPrice}
                    onChange={event => setTestPrice(event.target.value)}
                    className="ios-input w-full rounded-md px-3 py-2 text-sm"
                    placeholder="价格，可留空"
                  />
                  <textarea
                    value={testDesc}
                    onChange={event => setTestDesc(event.target.value)}
                    className="ios-input min-h-24 w-full rounded-md px-3 py-2 text-sm"
                    placeholder="商品介绍"
                  />
                  <button
                    type="button"
                    onClick={handleTest}
                    disabled={testing}
                    className="ios-btn-secondary flex w-full items-center justify-center gap-2 rounded-md px-4 py-2 text-sm"
                  >
                    {testing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                    {testing ? '生成中，可能需要一两分钟' : '测试，不发送'}
                  </button>
                  {testReply && (
                    <div className="rounded-md bg-gray-50 px-3 py-3 text-sm text-gray-800">{testReply}</div>
                  )}
                </div>
              </section>
            </div>
          </div>
        </>
      )}
    </div>
  );
};

export default NewAIReply;
