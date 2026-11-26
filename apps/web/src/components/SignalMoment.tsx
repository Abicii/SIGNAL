type Props = { type: string; message: string };

export function SignalMoment({ type, message }: Props) {
  return (
    <aside className="signal-moment" data-type={type}>
      <div className="moment-mark" aria-hidden="true">✳</div>
      <div>
        <p className="eyebrow">A SIGNAL MOMENT</p>
        <p className="moment-message">“{message}”</p>
      </div>
    </aside>
  );
}
