import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { describe, expect, it } from 'vitest';
import { AppComponent } from './app.component';
import { routes } from './app.routes';

describe('AppComponent', () => {
  async function setup() {
    await TestBed.configureTestingModule({
      imports: [AppComponent],
      providers: [provideRouter(routes)],
    }).compileComponents();

    const fixture = TestBed.createComponent(AppComponent);
    const router = TestBed.inject(Router);
    return { fixture, router };
  }

  it('creates the root application shell', async () => {
    const { fixture } = await setup();
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('redirects root path to /login and renders login placeholder', async () => {
    const { fixture, router } = await setup();
    await router.navigateByUrl('/');
    fixture.detectChanges();

    expect(router.url).toBe('/login');
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('h1')?.textContent).toContain('OnTrack');
    expect(host.querySelector('a')?.getAttribute('href')).toBe('/privacy-and-terms-of-use');
  });

  it('renders /privacy-and-terms-of-use with back-to-login link', async () => {
    const { fixture, router } = await setup();
    await router.navigateByUrl('/privacy-and-terms-of-use');
    fixture.detectChanges();

    expect(router.url).toBe('/privacy-and-terms-of-use');
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('h1')?.textContent).toContain('Privacy & Terms of Use');
    expect(host.querySelector('a')?.getAttribute('href')).toBe('/login');
  });

  it('renders 404 placeholder for unknown wildcard routes', async () => {
    const { fixture, router } = await setup();
    await router.navigateByUrl('/non-existent-route');
    fixture.detectChanges();

    expect(router.url).toBe('/non-existent-route');
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('h1')?.textContent).toContain('Page not found!');
    expect(host.querySelector('a')?.getAttribute('href')).toBe('/');
  });
});
