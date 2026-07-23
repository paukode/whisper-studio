import { useUIStore } from '@/stores/uiStore';
import { Toast } from './Toast';

export const ToastContainer: React.FC = () => {
  const toasts = useUIStore((state) => state.toasts);
  const dismissToast = useUIStore((state) => state.dismissToast);

  if (toasts.length === 0) return null;

  return (
    <div className="whisper-toast-container" aria-live="polite">
      {toasts.map((toast) => (
        <Toast
          key={toast.id}
          id={toast.id}
          type={toast.type}
          title={toast.title}
          message={toast.message}
          duration={toast.duration}
          count={toast.count}
          leaving={toast.leaving}
          shownAt={toast.shownAt}
          action={toast.action}
          onClose={dismissToast}
        />
      ))}
    </div>
  );
};
