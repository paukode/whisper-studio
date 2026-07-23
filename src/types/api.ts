export class ApiError extends Error {
  public readonly status: number;
  public readonly url: string;
  public readonly method: string;

  constructor(status: number, message: string, url: string, method: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.url = url;
    this.method = method;
  }
}
