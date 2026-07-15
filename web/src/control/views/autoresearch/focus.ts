export const withoutAutoresearchFocus = (params: URLSearchParams) => {
  const next = new URLSearchParams(params);
  next.delete("focus");
  return next;
};
