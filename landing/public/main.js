(function () {
  const toastEl = document.querySelector('[data-toast]');
  function toast(title, body) {
    if (!toastEl) return;
    toastEl.innerHTML = `<div><strong>${title}</strong>${body ? `<div class="muted">${body}</div>` : ''}</div>`;
    toastEl.classList.add('is-show');
    window.clearTimeout(toastEl.__t);
    toastEl.__t = window.setTimeout(() => toastEl.classList.remove('is-show'), 4200);
  }

  const burger = document.getElementById('burger');
  const mobileMenu = document.getElementById('mobileMenu');
  if (burger && mobileMenu) {
    burger.addEventListener('click', () => {
      mobileMenu.classList.toggle('open');
    });
    mobileMenu.querySelectorAll('a, button').forEach(link => {
      link.addEventListener('click', () => mobileMenu.classList.remove('open'));
    });
  }

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.15, rootMargin: '0px 0px -40px 0px' });
  document.querySelectorAll('.reveal').forEach((el) => observer.observe(el));

  const modal = document.getElementById('leadModal');
  const ctaField = document.querySelector('[data-cta-field]');
  const form = document.querySelector('[data-lead-form]');
  const err = document.querySelector('[data-form-error]');

  function openModal(ctaText) {
    if (!modal) return;
    if (ctaField) ctaField.value = ctaText || '';
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    const firstInput = modal.querySelector('input[name="name"]');
    if (firstInput) setTimeout(() => firstInput.focus(), 30);
  }

  function closeModal() {
    if (!modal) return;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
  }

  document.querySelectorAll('[data-open-modal]').forEach((btn) => {
    btn.addEventListener('click', () => openModal(btn.getAttribute('data-cta') || btn.textContent.trim()));
  });

  if (modal) {
    modal.querySelectorAll('[data-close-modal]').forEach((el) => {
      el.addEventListener('click', closeModal);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && modal.classList.contains('is-open')) closeModal();
    });
  }

  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (err) err.textContent = '';

      const fd = new FormData(form);
      const email = (fd.get('email') || '').toString().trim();
      if (!email || !email.includes('@')) {
        if (err) err.textContent = 'Проверьте email.';
        return;
      }

      const submitBtn = form.querySelector('button[type="submit"]');
      const oldText = submitBtn ? submitBtn.textContent : '';
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Отправляем…';
      }

      try {
        const res = await fetch(form.getAttribute('action') || '/api/lead', {
          method: 'POST',
          body: fd,
          headers: { 'Accept': 'application/json' }
        });

        if (!res.ok) {
          let msg = 'Не удалось отправить заявку.';
          try {
            const j = await res.json();
            if (j && j.detail) msg = j.detail;
          } catch (_) {}
          if (err) err.textContent = msg;
          toast('Ошибка', msg);
          return;
        }

        closeModal();
        toast('Готово', 'Заявка отправлена. Мы свяжемся с вами.');
        window.location.href = '/success';
      } catch (ex) {
        const msg = 'Сеть недоступна. Попробуйте ещё раз.';
        if (err) err.textContent = msg;
        toast('Ошибка', msg);
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = oldText || 'Отправить заявку';
        }
      }
    });
  }
})();
