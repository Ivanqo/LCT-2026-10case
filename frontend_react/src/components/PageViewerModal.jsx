import { useEffect, useState } from 'react';
import { fetchEvidencePageImage } from '../api.js';

export default function PageViewerModal({ fragment, onClose }) {
  const [src, setSrc] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let objectUrl = null;
    let cancelled = false;
    setSrc(null);
    setError('');
    fetchEvidencePageImage(fragment.id)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setSrc(url);
      })
      .catch((err) => !cancelled && setError(err.message || 'Не удалось загрузить страницу'));
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [fragment.id]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-box image-modal" onClick={(e) => e.stopPropagation()}>
        <h3>
          {fragment.file || 'Документ'} • стр. {fragment.page}
        </h3>
        {!src && !error && (
          <div className="row" style={{ padding: '30px 0', justifyContent: 'center' }}>
            <span className="spinner" /> <span className="muted">Загружаю страницу с выделением…</span>
          </div>
        )}
        {error && <div className="error-banner"><span>{error}</span></div>}
        {src && <img src={src} alt={`${fragment.file || 'страница'} стр. ${fragment.page}`} />}
        <div className="modal-actions">
          <button className="btn" type="button" onClick={onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  );
}
