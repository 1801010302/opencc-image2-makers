import { useState, useCallback, useEffect, useRef } from 'react';
import { sendMessageStream, fetchConversationHistory, stopAgent } from './api';
import styles from './App.module.css';

const TOOL_IDS = ['generate_image'] as const;
const LAMP_LABELS: Record<string, string> = {
  generate_image: '🖼️ 正在生成图片',
};
const LAMP_ICONS: Record<string, string> = {
  generate_image: '🖼️',
};

const CONVERSATION_KEY = 'opencc_img_conv_id';
const IMG_PREVIEW_KEY = 'opencc_img_previews';

function getOrCreateConvId(): string {
  const cached = localStorage.getItem(CONVERSATION_KEY);
  if (cached) return cached;
  const id = crypto.randomUUID();
  localStorage.setItem(CONVERSATION_KEY, id);
  return id;
}

interface ImagePreview {
  path: string;
  timestamp: number;
  prompt: string;
}

function App() {
  const [messages, setMessages] = useState<{id: string; role: string; content: string}[]>([]);
  const [lamps, setLamps] = useState<{id: string; active: boolean}[]>([]);
  const [loading, setLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [images, setImages] = useState<ImagePreview[]>(() => {
    try { return JSON.parse(localStorage.getItem(IMG_PREVIEW_KEY) || '[]'); } catch { return []; }
  });
  const [activeTab, setActiveTab] = useState<'chat' | 'gallery'>('chat');
  const [convId, setConvId] = useState(getOrCreateConvId);
  const botMsgIdRef = useRef('');
  const abortCtrlRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setLamps(TOOL_IDS.map(id => ({ id, active: false })));
  }, []);

  const updateBotMessage = useCallback((updater: (c: string) => string) => {
    setMessages(prev => prev.map(m =>
      m.id === botMsgIdRef.current ? { ...m, content: updater(m.content) } : m
    ));
  }, []);

  const finishStream = useCallback(() => { setLoading(false); abortCtrlRef.current = null; }, []);

  const handleSend = useCallback((text: string) => {
    const userMsg = { id: crypto.randomUUID(), role: 'user', content: text };
    const botId = crypto.randomUUID();
    botMsgIdRef.current = botId;
    setMessages(prev => [...prev, userMsg, { id: botId, role: 'assistant', content: '' }]);
    setLoading(true);

    const ctrl = sendMessageStream(text, {
      onTextDelta(delta) {
        updateBotMessage(c => c + delta);
      },
      onToolCalled(toolName) {
        setLamps(prev => prev.map(l => l.id === toolName ? { ...l, active: true } : l));
        setTimeout(() => setLamps(prev => prev.map(l => ({ ...l, active: false }))), 2000);
      },
      onDone() { finishStream(); },
      onError(err) {
        updateBotMessage(c => c || `出错了: ${err.message}`);
        finishStream();
      },
    }, convId);
    abortCtrlRef.current = ctrl;
  }, [convId, updateBotMessage, finishStream]);

  const handleStop = useCallback(() => {
    abortCtrlRef.current?.abort();
    abortCtrlRef.current = null;
    updateBotMessage(c => c ? c + '\n\n⏹ 已停止' : '⏹ 已停止');
    setLoading(false);
  }, [updateBotMessage]);

  const handleClear = useCallback(() => {
    abortCtrlRef.current?.abort();
    abortCtrlRef.current = null;
    const newId = crypto.randomUUID();
    localStorage.setItem(CONVERSATION_KEY, newId);
    setConvId(newId);
    setMessages([]);
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchConversationHistory(convId).then(hist => {
      if (hist.length > 0) setMessages(hist.map(m => ({ id: m.id, role: m.role, content: m.content })));
    }).finally(() => setHistoryLoading(false));
  }, []);

  const addImageToGallery = useCallback((path: string, prompt: string) => {
    const newImg = { path, timestamp: Date.now(), prompt };
    setImages(prev => {
      const next = [newImg, ...prev].slice(0, 20);
      localStorage.setItem(IMG_PREVIEW_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  return (
    <div className={styles.app}>
      <aside className={styles.sidebar}>
        <div className={styles.sidebarHeader}>
          <span className={styles.logo}>🎨</span>
          <span className={styles.sidebarTitle}>OpenCC Image2</span>
        </div>
        <nav className={styles.nav}>
          <button className={`${styles.navBtn} ${activeTab === 'chat' ? styles.navBtnActive : ''}`} onClick={() => setActiveTab('chat')}>
            💬 对话
          </button>
          <button className={`${styles.navBtn} ${activeTab === 'gallery' ? styles.navBtnActive : ''}`} onClick={() => setActiveTab('gallery')}>
            🖼️ 图库
          </button>
        </nav>
        {activeTab === 'gallery' && (
          <div className={styles.gallery}>
            {images.length === 0 && <p className={styles.empty}>暂无图片</p>}
            {images.map((img, i) => (
              <div key={i} className={styles.galleryItem}>
                <div className={styles.galleryImg} style={{background: '#333'}}>
                  <img src={img.path} alt={img.prompt} />
                </div>
                <p className={styles.galleryPrompt}>{img.prompt}</p>
              </div>
            ))}
          </div>
        )}
        <button className={styles.clearBtn} onClick={handleClear}>🗑 清空对话</button>
      </aside>

      <main className={styles.main}>
        <header className={styles.header}>
          <h1 className={styles.title}>OpenCC Image2</h1>
          <p className={styles.subtitle}>用文字描述你想要的图片，我来生成</p>
        </header>

        <div className={styles.chatArea}>
          {historyLoading && <div className={styles.loading}>加载中...</div>}
          {!historyLoading && messages.length === 0 && (
            <div className={styles.welcome}>
              <p>✨ 告诉我你想画什么</p>
              <p className={styles.hint}>支持中文、英文描述，也可以上传参考图</p>
            </div>
          )}
          {messages.map(m => (
            <div key={m.id} className={m.role === 'user' ? styles.userMsg : styles.botMsg}>
              <div className={styles.bubble}>{m.content}</div>
            </div>
          ))}
          {loading && (
            <div className={styles.botMsg}>
              <div className={styles.bubble}>思考中...</div>
            </div>
          )}
        </div>

        <div className={styles.toolLamps}>
          {lamps.map(lamp => (
            <div key={lamp.id} className={`${styles.lamp} ${lamp.active ? styles.lampActive : ''}`}>
              <span>{LAMP_ICONS[lamp.id] || '🔧'}</span>
              <span>{LAMP_LABELS[lamp.id]}</span>
            </div>
          ))}
        </div>

        <ChatInput onSend={handleSend} onStop={handleStop} loading={loading} />
      </main>
    </div>
  );
}

function ChatInput({ onSend, onStop, loading }: { onSend: (t: string) => void; onStop: () => void; loading: boolean }) {
  const [text, setText] = useState('');
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim() || loading) return;
    onSend(text.trim());
    setText('');
  };
  return (
    <form className={styles.chatInput} onSubmit={handleSubmit}>
      <input
        className={styles.input}
        value={text}
        onChange={e => setText(e.target.value)}
        placeholder="描述你想要生成的图片..."
        disabled={loading}
      />
      <button type="submit" className={styles.sendBtn} disabled={loading || !text.trim()}>
        {loading ? '⏹' : '🎨'}
      </button>
    </form>
  );
}

export default App;
