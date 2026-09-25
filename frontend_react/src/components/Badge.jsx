export default function Badge({ tone = 'neutral', children }) {
  return (
    <span className={`badge ${tone}`}>
      <span className="dot" />
      {children}
    </span>
  );
}
