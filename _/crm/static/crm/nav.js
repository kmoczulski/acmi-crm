/**
 * ACMI CRM - Global Navigation & Mobile Drawer Controller
 * Location: crm/static/crm/nav.js
 */

document.addEventListener('DOMContentLoaded', () => {
  // Shell Element Targets
  const sidebar = document.getElementById('appSidebar');
  const collapseBtn = document.getElementById('sidebarCollapseBtn');
  const bottomSheet = document.getElementById('bottomSheetBackdrop');
  const moreBtn = document.getElementById('moreModulesBtn');
  const closeSheetBtn = document.getElementById('bottomSheetCloseBtn');
  const drawerOpenBtn = document.getElementById('mobileDrawerOpenBtn');
  const userMenuBtn = document.getElementById('userMenuBtn');
  const userDropdown = document.querySelector('.user-dropdown-menu');

  // 1. Sidebar Collapse (Desktop Persistence)
  if (collapseBtn && sidebar) {
    const isCollapsed = localStorage.getItem('acmi_sidebar_collapsed') === 'true';
    if (isCollapsed) {
      sidebar.classList.add('collapsed');
    }

    collapseBtn.addEventListener('click', () => {
      sidebar.classList.toggle('collapsed');
      localStorage.setItem('acmi_sidebar_collapsed', sidebar.classList.contains('collapsed'));
    });
  }

  // 2. Mobile Bottom Sheet Drawer Controls
  function openBottomSheet() {
    if (!bottomSheet) return;
    bottomSheet.classList.add('open');
    bottomSheet.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function closeBottomSheet() {
    if (!bottomSheet) return;
    bottomSheet.classList.remove('open');
    bottomSheet.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  if (moreBtn) moreBtn.addEventListener('click', openBottomSheet);
  if (drawerOpenBtn) drawerOpenBtn.addEventListener('click', openBottomSheet);
  if (closeSheetBtn) closeSheetBtn.addEventListener('click', closeBottomSheet);

  if (bottomSheet) {
    bottomSheet.addEventListener('click', (e) => {
      if (e.target === bottomSheet) closeBottomSheet();
    });
  }

  // 3. User Avatar / Profile Dropdown Toggle
  if (userMenuBtn && userDropdown) {
    userMenuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = userDropdown.classList.toggle('show');
      userMenuBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });

    document.addEventListener('click', (e) => {
      if (!userDropdown.contains(e.target) && e.target !== userMenuBtn) {
        userDropdown.classList.remove('show');
        userMenuBtn.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // 4. Global Keyboard Navigation (Cmd+K / Esc)
  window.addEventListener('keydown', (e) => {
    // Quick search trigger
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      const searchBtn = document.getElementById('globalSearchBtn');
      if (searchBtn) searchBtn.click();
    }

    // Dismiss open sheets and menus
    if (e.key === 'Escape') {
      if (bottomSheet?.classList.contains('open')) {
        closeBottomSheet();
      }
      if (userDropdown?.classList.contains('show')) {
        userDropdown.classList.remove('show');
        userMenuBtn?.setAttribute('aria-expanded', 'false');
      }
    }
  });
});