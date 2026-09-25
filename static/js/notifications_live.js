function toggleNotifDropdown(e) {
    if (e) e.stopPropagation();
    const dropdown = document.getElementById('notifDropdown');
    if (dropdown) {
        dropdown.classList.toggle('hidden');
    }
}

document.addEventListener('click', function(e) {
    const btn = document.getElementById('notifBellBtn');
    const dropdown = document.getElementById('notifDropdown');
    if (dropdown && !dropdown.contains(e.target) && btn && !btn.contains(e.target)) {
        dropdown.classList.add('hidden');
    }
});

function getNotifTheme(type, title) {
    const titleLower = (title || '').toLowerCase();
    const typeLower = (type || '').toLowerCase();

    if (typeLower.includes('match') || titleLower.includes('match')) {
        return { icon: 'auto_awesome', bg: 'bg-emerald-100 text-emerald-600' };
    } else if (titleLower.includes('claim request') || typeLower.includes('claim_received')) {
        return { icon: 'person', bg: 'bg-blue-100 text-blue-600' };
    } else if (titleLower.includes('handover') || titleLower.includes('security')) {
        return { icon: 'shield', bg: 'bg-purple-100 text-purple-600' };
    } else if (typeLower.includes('chat') || titleLower.includes('message')) {
        return { icon: 'chat', bg: 'bg-amber-100 text-amber-600' };
    } else if (titleLower.includes('points earned')) {
        return { icon: 'military_tech', bg: 'bg-emerald-100 text-emerald-600' };
    } else if (titleLower.includes('report updated')) {
        return { icon: 'notifications', bg: 'bg-sky-100 text-sky-600' };
    } else if (titleLower.includes('rejected')) {
        return { icon: 'cancel', bg: 'bg-red-100 text-red-600' };
    } else if (titleLower.includes('report created')) {
        return { icon: 'article', bg: 'bg-purple-100 text-purple-600' };
    } else if (titleLower.includes('welcome')) {
        return { icon: 'inventory_2', bg: 'bg-emerald-100 text-emerald-600' };
    } else if (titleLower.includes('community points')) {
        return { icon: 'emoji_events', bg: 'bg-amber-100 text-amber-600' };
    } else if (titleLower.includes('accepted')) {
        return { icon: 'verified_user', bg: 'bg-pink-100 text-pink-600' };
    }
    
    return { icon: 'notifications', bg: 'bg-emerald-100 text-emerald-600' };
}

async function fetchLiveNotifications() {
    try {
        const res = await fetch('/api/notifications');
        if (!res.ok) return;
        const data = await res.json();
        
        const badge = document.getElementById('notifBadge');
        const unreadBadge = document.getElementById('notifUnreadBadge');
        const list = document.getElementById('notifList');
        
        if (badge) {
            if (data.unread_count > 0) {
                badge.innerText = data.unread_count > 99 ? '99+' : data.unread_count;
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }
        }
        
        if (unreadBadge) {
            unreadBadge.innerText = `${data.unread_count} Unread`;
        }

        if (list && data.notifications) {
            if (data.notifications.length === 0) {
                list.innerHTML = '<div class="p-6 text-center text-xs text-gray-400 italic">No notifications yet.</div>';
                return;
            }

            list.innerHTML = data.notifications.map(n => {
                const theme = getNotifTheme(n.type, n.title);
                const unreadBg = !n.is_read ? 'bg-emerald-50/40' : 'bg-white';
                
                return `
                    <div onclick="handleNotifClick('${n.id}', '${n.action_url}')" class="p-3.5 ${unreadBg} hover:bg-gray-50/80 cursor-pointer transition-colors flex items-start gap-3 border-b border-gray-100/60 last:border-0">
                        <div class="w-9 h-9 rounded-full ${theme.bg} flex items-center justify-center shrink-0 mt-0.5 shadow-xs">
                            <span class="material-symbols-outlined text-base icon-fill">${theme.icon}</span>
                        </div>
                        <div class="flex-1 min-w-0">
                            <div class="flex items-center justify-between gap-2">
                                <h5 class="text-xs font-extrabold text-gray-900 truncate">${escapeHtml(n.title || 'Notification')}</h5>
                                <span class="text-[10px] text-gray-400 font-medium shrink-0">${n.time_ago}</span>
                            </div>
                            <div class="text-xs text-gray-600 font-medium leading-relaxed mt-0.5">${n.message}</div>
                        </div>
                        ${!n.is_read ? '<span class="w-2 h-2 rounded-full bg-emerald-500 shrink-0 mt-2"></span>' : ''}
                    </div>
                `;
            }).join('');
        }
    } catch (err) {
        console.error('Notification error:', err);
    }
}

function escapeHtml(str) {
    return (str || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function handleNotifClick(notifId, actionUrl) {
    try {
        await fetch(`/api/notifications/mark-read/${notifId}`, { method: 'POST' });
    } catch(e) {}
    window.location.href = actionUrl || '/user/history';
}

document.addEventListener('DOMContentLoaded', () => {
    fetchLiveNotifications();
    setInterval(fetchLiveNotifications, 10000);
});

