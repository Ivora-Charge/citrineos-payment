import * as React from 'react';
import IvoraMark from '../assets/images/ivora-mark.png';

/**
 * Ivora Charge lockup: the elephant mark from ivoracharge.com plus the
 * serif wordmark, matching the website's nav-brand. `size` controls the
 * mark height in px. Set `wordmark={false}` for mark-only.
 */
export default function Logo({ size = 28, wordmark = true }) {
  return (
    <div className="ivora-logo">
      <img
        className="ivora-logo__badge"
        src={IvoraMark}
        alt="Ivora Charge"
        style={{ height: size }}
      />
      {wordmark && (
        <div className="ivora-logo__wm">
          Ivora<span>Charge</span>
        </div>
      )}
    </div>
  );
}
