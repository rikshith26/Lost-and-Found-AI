(function () {
    /**
     * Device class toggler
     * Keeps a mobile-first experience while allowing desktop layouts.
     */
    function checkDevice() {
        const isMobile = window.innerWidth <= 768;
        document.body.classList.toggle('is-mobile-view', isMobile);
        document.body.classList.toggle('is-desktop-view', !isMobile);
    }

    // Expose for external calls
    window.checkDevice = checkDevice;

    // Run on boot
    checkDevice();
    
    // Listen for resize (e.g. pivoting device or resizing window)
    window.addEventListener('resize', checkDevice);
    
    // Ensure it runs after full DOM loads for safety
    window.addEventListener('load', checkDevice);
})();
