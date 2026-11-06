// A person's own account: name, email, and password. Every role has this page.
import { api, clear, h, live, toast } from './core.js';

function field(label, control, hint) {
  return h('label', { class: 'field' }, h('span', {}, label), control, hint ? h('small', {}, hint) : null);
}

function outcome(host, ok, text) {
  clear(host, h('p', { class: `form-result ${ok ? 'ok' : 'bad'}`, role: ok ? 'status' : 'alert' }, text));
}

export function mount(container) {
  const me = live.me;
  const name = h('input', { class: 'input', required: true, maxlength: 120, autocomplete: 'name', value: me.name });
  const nameResult = h('div');
  const email = h('input', { class: 'input', type: 'email', required: true, autocomplete: 'email', value: me.email });
  const emailPassword = h('input', { class: 'input', type: 'password', required: true, autocomplete: 'current-password' });
  const emailResult = h('div');
  const current = h('input', { class: 'input', type: 'password', required: true, autocomplete: 'current-password' });
  const next = h('input', { class: 'input', type: 'password', required: true, minlength: 12, autocomplete: 'new-password' });
  const passwordResult = h('div');

  async function saveName(event) {
    event.preventDefault();
    try {
      await api('/api/auth/me', { method: 'PATCH', body: { name: name.value.trim() } });
      live.me.name = name.value.trim();
      live.emit('me');
      outcome(nameResult, true, 'Name saved.');
    } catch (error) { outcome(nameResult, false, error.message); }
  }

  async function saveEmail(event) {
    event.preventDefault();
    try {
      const saved = await api('/api/auth/me', { method: 'PATCH', body: { email: email.value.trim(), current_password: emailPassword.value } });
      emailPassword.value = '';
      if (saved.pending_email) {
        outcome(emailResult, true, `We sent a link to ${saved.pending_email}. You keep signing in with ${me.email} until you open it.`);
      } else {
        live.me = await api('/api/auth/me');
        live.emit('me');
        outcome(emailResult, true, 'Email saved. Use it the next time you sign in.');
      }
    } catch (error) { outcome(emailResult, false, error.message); }
  }

  async function savePassword(event) {
    event.preventDefault();
    try {
      await api('/api/auth/change-password', { method: 'POST', body: { current_password: current.value, new_password: next.value } });
      current.value = '';
      next.value = '';
      toast('Password changed');
      outcome(passwordResult, true, 'Password changed. You were signed out on your other devices.');
    } catch (error) { outcome(passwordResult, false, error.message); }
  }

  clear(container, h('div', { class: 'page' },
    h('h1', { class: 'page-title' }, 'Your account'),
    h('p', { class: 'page-lede' }, `You’re ${/^[aeio]/.test(me.role) ? 'an' : 'a'} ${me.role} in ${me.org_name}.`),
    h('div', { class: 'account-forms' },
      h('form', { class: 'stack', onSubmit: saveName }, h('h2', {}, 'Name'), field('Your name', name, 'Other people see this.'), nameResult, h('button', { class: 'button primary compact', type: 'submit' }, 'Save name')),
      me.uses_sso
        ? h('section', { class: 'stack' }, h('h2', {}, 'Email'), h('p', { class: 'body-text' }, `${me.email}. Your company’s single sign-on manages this address.`))
        : h('form', { class: 'stack', onSubmit: saveEmail }, h('h2', {}, 'Email'),
          field('Email address', email, 'You sign in with this.'), field('Current password', emailPassword, 'Needed to change your email.'),
          me.pending_email ? h('p', { class: 'notice' }, `Waiting for you to confirm ${me.pending_email}.`) : null,
          emailResult, h('button', { class: 'button primary compact', type: 'submit' }, 'Change email')),
      me.uses_sso ? null : h('form', { class: 'stack', onSubmit: savePassword }, h('h2', {}, 'Password'),
        field('Current password', current), field('New password', next, 'At least 12 characters. Avoid your name and common words.'),
        passwordResult, h('button', { class: 'button primary compact', type: 'submit' }, 'Change password')))));
  return () => {};
}
